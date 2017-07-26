import os
import tempfile
import shutil
from itertools import chain
from collections import defaultdict
from copy import copy
from nipype.pipeline import engine as pe
from .nodes import Node, JoinNode, MapNode, DEFAULT_MEMORY, DEFAULT_WALL_TIME
from nipype.interfaces.utility import IdentityInterface
from nianalysis.interfaces.utils import Merge
from logging import getLogger
from nianalysis.exceptions import (
    NiAnalysisDatasetNameError, NiAnalysisError, NiAnalysisMissingDatasetError)
from nianalysis.data_formats import get_converter_node
from nianalysis.interfaces.iterators import (
    InputSessions, PipelineReport, InputSubjects, SubjectReport,
    VisitReport, SubjectSessionReport, SessionReport)
from nianalysis.utils import INPUT_SUFFIX, OUTPUT_SUFFIX
from nianalysis.exceptions import NiAnalysisUsageError
from nianalysis.plugins.slurmgraph import SLURMGraphPlugin
from rdflib import plugin


logger = getLogger('NIAnalysis')


class Pipeline(object):
    """
    Basically a wrapper around a NiPype workflow to keep track of the inputs
    and outputs a little better and provide some convenience functions related
    to the Study objects.

    Parameters
    ----------
    name : str
        The name of the pipeline
    study : Study
        The study from which the pipeline was created
    inputs : List[BaseFile]
        The list of input datasets required for the pipeline
        un/processed datasets, and the options used to generate them for
        unprocessed datasets
    outputs : List[ProcessedFile]
        The list of outputs (hard-coded names for un/processed datasets)
    default_options : Dict[str, *]
        Default options that are used to construct the pipeline. They can
        be overriden by values provided to they 'options' keyword arg
    citations : List[Citation]
        List of citations that describe the workflow and should be cited in
        publications
    requirements : List[Requirement]
        List of external package requirements (e.g. FSL, MRtrix) required
        by the pipeline
    version : int
        A version number for the pipeline to be incremented whenever the output
        of the pipeline
    approx_runtime : float
        Approximate run time in minutes. Should be conservative so that
        it can be used to set time limits on HPC schedulers
    min_nthreads : int
        The minimum number of threads the pipeline requires to run
    max_nthreads : int
        The maximum number of threads the pipeline can use effectively.
        Use None if there is no effective limit
    options : Dict[str, *]
        Options that effect the output of the pipeline that override the
        default options. Extra options that are not in the default_options
        dictionary are ignored
    """

    iterfields = ('subject_id', 'visit_id')

    def __init__(self, study, name, inputs, outputs, description,
                 default_options, citations, version, options={}):
        self._name = name
        self._study = study
        self._workflow = pe.Workflow(name=name)
        self._version = int(version)
        # Set up inputs
        self._check_spec_names(inputs, 'input')
        if any(i.name in self.iterfields for i in inputs):
            raise NiAnalysisError(
                "Cannot have a dataset spec named '{}' as it clashes with "
                "iterable field of that name".format(i.name))
        self._inputs = inputs
        self._inputnode = self.create_node(
            IdentityInterface(fields=(
                tuple(self.input_names) + self.iterfields)),
            name="inputnode", wall_time=1, memory=1000)
        # Set up outputs
        self._check_spec_names(outputs, 'output')
        self._outputs = defaultdict(list)
        for output in outputs:
            mult = self._study.dataset_spec(output).multiplicity
            self._outputs[mult].append(output)
        self._outputnodes = {}
        for mult in self._outputs:
            self._outputnodes[mult] = self.create_node(
                IdentityInterface(
                    fields=[o.name for o in self._outputs[mult]]),
                name="{}_outputnode".format(mult), wall_time=1,
                memory=1000)
        # Create sets of unconnected inputs/outputs
        self._unconnected_inputs = set(self.input_names)
        self._unconnected_outputs = set(self.output_names)
        assert len(inputs) == len(self._unconnected_inputs), (
            "Duplicate inputs found in '{}'"
            .format("', '".join(self.input_names)))
        assert len(outputs) == len(self._unconnected_outputs), (
            "Duplicate outputs found in '{}'"
            .format("', '".join(self.output_names)))
        self._citations = citations
        self._default_options = default_options
        # Copy default options to options and then update it with specific
        # options passed to this pipeline
        self._options = copy(default_options)
        for k, v in options.iteritems():
            if k in self.options:
                self.options[k] = v
        self._description = description

    def _check_spec_names(self, specs, spec_type):
        # Check for unrecognised inputs/outputs
        unrecognised = set(s for s in specs
                           if s.name not in self.study.dataset_spec_names())
        if unrecognised:
            raise NiAnalysisError(
                "'{}' are not valid {} names for {} study ('{}')"
                .format("', '".join(u.name for u in unrecognised), spec_type,
                        self.study.__class__.__name__,
                        "', '".join(self.study.dataset_spec_names())))

    def __repr__(self):
        return "Pipeline(name='{}')".format(self.name)

    @property
    def requires_gpu(self):
        return False  # FIXME: Need to implement this

    @property
    def max_memory(self):
        return 4000

    @property
    def wall_time(self):
        return '7-00:00:00'  # Max amount

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self._name == other._name and
            self._study == other._study and
            self._inputs == other._inputs and
            self._outputs == other._outputs and
            self._options == other._options and
            self._citations == other._citations)

    def __ne__(self, other):
        return not (self == other)

    def run(self, work_dir=None, plugin='Linear', mode='parallel', **kwargs):
        """
        Connects pipeline to archive and runs it on the local workstation

        Parameters
        ----------
        subject_ids : List[str]
            The subset of subject IDs to process. If None all available will be
            reprocessed
        visit_ids: List[str]
            The subset of visit IDs to process. If None all available will be
            reprocessed
        work_dir : str
            A directory in which to run the nipype workflows
        reprocess: True|False|'all'
            A flag which determines whether to rerun the processing for this
            step. If set to 'all' then pre-requisite pipelines will also be
            reprocessed.
        """
        complete_workflow = pe.Workflow(name=self.name, base_dir=work_dir)
        self.connect_to_archive(complete_workflow, **kwargs)
        # Run the workflow
        return complete_workflow.run()

    def submit(self, work_dir, scheduler='slurm', email=None,
               mail_on=('END', 'FAIL'), **kwargs):
        """
        Submits a pipeline to a scheduler que for processing

        Parameters
        ----------
        scheduler : str
            Name of the scheduler to submit the pipeline to
        """
        if email is None:
            try:
                email = os.environ['EMAIL']
            except KeyError:
                raise NiAnalysisError(
                    "'email' needs to be provided if 'EMAIL' environment "
                    "variable not set")
        if scheduler == 'slurm':
            args = [('mail-user', email)]
            for mo in mail_on:
                args.append(('mail-type', mo))
            plugin_args = {
                'sbatch_args': ' '.join('--{}={}'.format(*a) for a in args)}
            plugin = SLURMGraphPlugin(plugin_args=plugin_args)
        else:
            raise NiAnalysisUsageError(
                "Unsupported scheduler '{}'".format(scheduler))
        complete_workflow = pe.Workflow(name=self.name, base_dir=work_dir)
        self.connect_to_archive(complete_workflow, **kwargs)
        return complete_workflow.run(plugin=plugin)

    def write_graph(self, fname, detailed=False, style='flat', complete=False):
        """
        Writes a graph of the pipeline to file

        Parameters
        ----------
        fname : str
            The filename for the saved graph
        detailed : bool
            Whether to save a detailed version of the graph or not
        style : str
            The style of the graph, can be one of can be one of
            'orig', 'flat', 'exec', 'hierarchical'
        complete : bool
            Whether to plot the complete graph including sources, sinks and
            prerequisite pipelines or just the current pipeline
        plot : bool
            Whether to load and plot the graph after it has been written
        """
        fname = os.path.expanduser(fname)
        orig_dir = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        os.chdir(tmpdir)
        if complete:
            workflow = pe.Workflow(name=self.name, base_dir=tmpdir)
            self.connect_to_archive(workflow)
            out_dir = os.path.join(tmpdir, self.name)
        else:
            workflow = self._workflow
            out_dir = tmpdir
        workflow.write_graph(graph2use=style)
        if detailed:
            graph_file = 'graph_detailed.dot.png'
        else:
            graph_file = 'graph.dot.png'
        os.chdir(orig_dir)
        shutil.move(os.path.join(out_dir, graph_file), fname)
        shutil.rmtree(tmpdir)

    def connect_to_archive(self, complete_workflow, subject_ids=None,
                           visit_ids=None, reprocess=False,
                           project=None, connected_prereqs=None):
        """
        Gets a data source and data sink from the archive for the requested
        sessions, connects them to the pipeline's NiPyPE workflow

        Parameters
        ----------
        subject_ids : List[str]
            The subset of subject IDs to process. If None all available will be
            reprocessed
        visit_ids: List[str]
            The subset of visit IDs for each subject to process. If None all
            available will be reprocessed
        work_dir : str
            A directory in which to run the nipype workflows
        reprocess: True|False|'all'
            A flag which determines whether to rerun the processing for this
            step. If set to 'all' then pre-requisite pipelines will also be
            reprocessed.
        project: Project
            Project info loaded from archive. It is typically only passed to
            runs of prerequisite pipelines to avoid having to re-query the
            archive. If None, the study info is loaded from the study
            archive.
        connected_prereqs: list(Pipeline, Node)
            Prerequisite pipelines that have already been connected to the
            workflow (prequisites of prerequisites) and their corresponding
            "report" nodes

        Returns
        -------
        report : ReportNode
            The final report node, which can be connected to subsequent
            pipelines
        """
        if connected_prereqs is None:
            connected_prereqs = {}
        # Check all inputs and outputs are connected
        self.assert_connected()
        # Get list of available subjects and their associated sessions/datasets
        # from the archive
        if project is None:
            project = self._study.archive.project(
                self._study._project_id, subject_ids=subject_ids,
                visit_ids=visit_ids)
        # Get list of sessions that need to be processed (i.e. if
        # they don't contain the outputs of this pipeline)
        sessions_to_process = self._sessions_to_process(
            project, visit_ids=visit_ids, reprocess=reprocess)
        if not sessions_to_process:
            logger.info(
                "All outputs of '{}' are already present in project archive, "
                "skipping".format(self.name))
            return None
        # Set up workflow to run the pipeline, loading and saving from the
        # archive
        complete_workflow.add_nodes([self._workflow])
        # Get iterator nodes over subjects and sessions to be processed
        subjects, sessions = self._subject_and_session_iterators(
            sessions_to_process, complete_workflow)
        # Prepend prerequisite pipelines to complete workflow if required
        if self.has_prerequisites:
            reports = []
            prereq_subject_ids = list(
                set(s.subject.id for s in sessions_to_process))
            for prereq in self.prerequisities:
                try:
                    (connected_prereq,
                     prereq_report) = connected_prereqs[prereq.name]
                    if connected_prereq != prereq:
                        raise NiAnalysisError(
                            "Name clash between {} and {} non-matching "
                            "prerequisite pipelines".format(connected_prereq,
                                                            prereq))
                    reports.append(prereq_report)
                except KeyError:
                    # NB: Even if reprocess==True, the prerequisite pipelines
                    # are not re-processed, they are only reprocessed if
                    # reprocess == 'all'
                    prereq_report = prereq.connect_to_archive(
                        complete_workflow=complete_workflow,
                        subject_ids=prereq_subject_ids,
                        visit_ids=visit_ids,
                        reprocess=(reprocess if reprocess == 'all' else False),
                        project=project,
                        connected_prereqs=connected_prereqs)
                    if prereq_report is not None:
                        connected_prereqs[prereq.name] = prereq, prereq_report
                        reports.append(prereq_report)
            if reports:
                prereq_reports = self.create_node(Merge(len(reports)),
                                                  'prereq_reports')
                for i, report in enumerate(reports, 1):
                    # Connect the output summary of the prerequisite to the
                    # pipeline to ensure that the prerequisite is run first.
                    complete_workflow.connect(
                        report, 'subject_session_pairs',
                        prereq_reports, 'in{}'.format(i))
                complete_workflow.connect(prereq_reports, 'out', subjects,
                                          'prereq_reports')
        try:
            # Create source and sinks from the archive
            source = self._study.archive.source(
                self.study.project_id,
                (self.study.dataset(i) for i in self.inputs),
                study_name=self.study.name,
                name='{}_source'.format(self.name))
        except NiAnalysisMissingDatasetError as e:
            raise NiAnalysisMissingDatasetError(
                str(e) + ", which is required for pipeline '{}'".format(
                    self.name))
        # Map the subject and visit IDs to the input node of the pipeline
        # for use in connect_subject_id and connect_visit_id
        complete_workflow.connect(sessions, 'subject_id',
                                  self.inputnode, 'subject_id')
        complete_workflow.connect(sessions, 'visit_id',
                                  self.inputnode, 'visit_id')
        # Connect the nodes of the wrapper workflow
        complete_workflow.connect(sessions, 'subject_id',
                                  source, 'subject_id')
        complete_workflow.connect(sessions, 'visit_id',
                                  source, 'visit_id')
        for inpt in self.inputs:
            # Get the dataset corresponding to the pipeline's input
            dataset = self.study.dataset(inpt.name)
            if dataset.format != inpt.format:
                # Insert a format converter node into the workflow if the
                # format of the dataset if it is not in the required format for
                # the study
                conv_node_name = '{}_{}_input_conversion'.format(self.name,
                                                                  inpt.name)
                dataset_source, dataset_name = get_converter_node(
                    dataset, dataset.name + OUTPUT_SUFFIX, inpt.format,
                    source, complete_workflow, conv_node_name)
            else:
                dataset_source = source
                dataset_name = dataset.name + OUTPUT_SUFFIX
            # Connect the dataset to the pipeline input
            complete_workflow.connect(dataset_source, dataset_name,
                                      self.inputnode, inpt.name)
        # Create a report node for holding a summary of all the sessions/
        # subjects that were sunk. This is used to connect with dependent
        # pipelines into one large connected pipeline.
        report = self.create_node(PipelineReport(), 'report')
        # Connect all outputs to the archive sink
        for mult, outputs in self._outputs.iteritems():
            # Create a new sink for each multiplicity level (i.e 'per_session',
            # 'per_subject', 'per_visit', or 'per_project')
            sink = self.study.archive.sink(
                self.study._project_id,
                (self.study.dataset(o) for o in outputs), mult,
                study_name=self.study.name,
                name='{}_{}_sink'.format(self.name, mult))
            sink.inputs.description = self.description
            sink.inputs.name = self._study.name
            if mult in ('per_session', 'per_subject'):
                complete_workflow.connect(sessions, 'subject_id',
                                          sink, 'subject_id')
            if mult in ('per_session', 'per_visit'):
                complete_workflow.connect(sessions, 'visit_id',
                                          sink, 'visit_id')
            for output in outputs:
                # Get the dataset spec corresponding to the pipeline's output
                dataset = self.study.dataset(output.name)
                # Skip datasets which are already input datasets
                if dataset.processed:
                    # Convert the format of the node if it doesn't match
                    if dataset.format != output.format:
                        conv_node_name = output.name + '_output_conversion'
                        output_node, node_dataset_name = get_converter_node(
                            output, output.name, dataset.format,
                            self._outputnodes[mult], complete_workflow,
                            conv_node_name)
                    else:
                        output_node = self._outputnodes[mult]
                        node_dataset_name = dataset.name
                    complete_workflow.connect(
                        output_node, node_dataset_name,
                        sink, dataset.name + INPUT_SUFFIX)
            self._connect_to_reports(
                sink, report, mult, subjects, sessions, complete_workflow)
        return report

    def _subject_and_session_iterators(self, sessions_to_process, workflow):
        """
        Generate an input node that iterates over the sessions and subjects
        that need to be processed.
        """
        # Create nodes to control the iteration over subjects and sessions in
        # the project
        subjects = self.create_node(InputSubjects(), 'subjects', wall_time=1,
                                    memory=1000)
        sessions = self.create_node(InputSessions(), 'sessions', wall_time=1,
                                    memory=1000)
        # Construct iterable over all subjects to process
        subjects_to_process = set(s.subject for s in sessions_to_process)
        subject_ids_to_process = set(s.id for s in subjects_to_process)
        subjects.iterables = ('subject_id',
                              tuple(s.id for s in subjects_to_process))
        # Determine whether the visit ids are the same for every subject,
        # in which case they can be set as a constant, otherwise they will
        # need to be specified for each subject separately
        session_subjects = defaultdict(set)
        for session in sessions_to_process:
            session_subjects[session.id].add(session.subject.id)
        if all(ss == subject_ids_to_process
               for ss in session_subjects.itervalues()):
            # All sessions are to be processed in every node, a simple second
            # layer of iterations on top of the subject iterations will
            # suffice. This allows re-combining on visit_id across subjects
            sessions.iterables = ('visit_id', session_subjects.keys())
        else:
            # visit IDs to be processed vary between subjects and so need
            # to be specified explicitly
            subject_sessions = defaultdict(list)
            for session in sessions_to_process:
                subject_sessions[session.subject.id].append(session.id)
            sessions.itersource = ('{}_subjects'.format(self.name),
                                   'subject_id')
            sessions.iterables = ('visit_id', subject_sessions)
        # Connect subject and session nodes together
        workflow.connect(subjects, 'subject_id', sessions, 'subject_id')
        return subjects, sessions

    def _connect_to_reports(self, sink, output_summary, mult, subjects,
                            sessions, workflow):
        """
        Connects the sink of the pipeline to an "Output Summary", which lists
        the subjects and sessions that were processed for the pipeline. There
        should be only one summary node instance per pipeline so it can be
        used to feed into the input of subsequent pipelines to ensure that
        they are executed afterwards.
        """
        if mult == 'per_session':
            session_outputs = JoinNode(
                SessionReport(), joinsource=sessions,
                joinfield=['subjects', 'sessions'],
                name=self.name + '_session_outputs', wall_time=1,
                memory=1000)
            subject_session_outputs = JoinNode(
                SubjectSessionReport(), joinfield='subject_session_pairs',
                joinsource=subjects,
                name=self.name + '_subject_session_outputs', wall_time=1,
                memory=1000)
            workflow.connect(sink, 'subject_id', session_outputs, 'subjects')
            workflow.connect(sink, 'visit_id', session_outputs, 'sessions')
            workflow.connect(session_outputs, 'subject_session_pairs',
                             subject_session_outputs, 'subject_session_pairs')
            workflow.connect(
                subject_session_outputs, 'subject_session_pairs',
                output_summary, 'subject_session_pairs')
        elif mult == 'per_subject':
            subject_output_summary = JoinNode(
                SubjectReport(), joinsource=subjects, joinfield='subjects',
                name=self.name + '_subject_summary_outputs', wall_time=1,
                memory=1000)
            workflow.connect(sink, 'subject_id',
                             subject_output_summary, 'subjects')
            workflow.connect(subject_output_summary, 'subjects',
                             output_summary, 'subjects')
        elif mult == 'per_visit':
            visit_output_summary = JoinNode(
                VisitReport(), joinsource=sessions, joinfield='sessions',
                name=self.name + '_visit_summary_outputs', wall_time=1,
                memory=1000)
            workflow.connect(sink, 'visit_id',
                             visit_output_summary, 'sessions')
            workflow.connect(visit_output_summary, 'sessions',
                             output_summary, 'visits')
        elif mult == 'per_project':
            workflow.connect(sink, 'project_id', output_summary, 'project')

    @property
    def has_prerequisites(self):
        return any(self._study.dataset(i).processed for i in self.inputs)

    @property
    def prerequisities(self):
        """
        Recursively append prerequisite pipelines along with their
        prerequisites onto the list of pipelines if they are not already
        present
        """
        # Loop through the inputs to the pipeline and add the instancemethods
        # for the pipelines to generate each of the processed inputs
        pipeline_getters = set()
        for input in self.inputs:  # @ReservedAssignment
            comp = self._study.dataset(input)
            if comp.processed:
                pipeline_getters.add(comp.pipeline)
        # Call pipeline instancemethods to study with provided options
        return (pg(self._study, **self.options) for pg in pipeline_getters)

    def _sessions_to_process(self, project, visit_ids=None, reprocess=False):
        """
        Check whether the outputs of the pipeline are present in all sessions
        in the project archive, and make a list of the sessions and subjects
        that need to be reprocessed if they aren't.

        Parameters
        ----------
        project : Project
            A representation of the project and associated subjects and
            sessions for the study's archive.
        visit_ids : list(str)
            Filter the visit IDs to process
        """
        all_subjects = list(project.subjects)
        all_sessions = list(chain(*[s.sessions for s in all_subjects]))
        if reprocess:
            return all_sessions
        sessions_to_process = set()
        # Define filter function
        def filter_sessions(sessions):  # @IgnorePep8
            if visit_ids is None:
                return sessions
            else:
                return (s for s in sessions if s.id in visit_ids)
        for output in self.outputs:
            dataset = self.study.dataset(output)
            # If there is a project output then all subjects and sessions need
            # to be reprocessed
            if dataset.multiplicity == 'per_project':
                if dataset.prefixed_name not in project.dataset_names:
                    return all_sessions
            elif dataset.multiplicity in ('per_subject', 'per_visit'):
                sessions_to_process.update(chain(*(
                    filter_sessions(sub.sessions) for sub in all_subjects
                    if dataset.prefixed_name not in sub.dataset_names)))
            elif dataset.multiplicity == 'per_session':
                sessions_to_process.update(filter_sessions(
                    s for s in all_sessions
                    if dataset.prefixed_name not in s.dataset_names))
            else:
                assert False, "Unrecognised multiplicity of {}".format(dataset)
        return list(sessions_to_process)

    def connect(self, *args, **kwargs):
        """
        Performs the connection in the wrapped NiPype workflow
        """
        self._workflow.connect(*args, **kwargs)

    def connect_input(self, spec_name, node, node_input):
        """
        Connects a study dataset_spec as an input to the provided node

        Parameters
        ----------
        spec_name : str
            Name of the study dataset spec to join to the node
        node : nipype.pipeline.BaseNode
            A NiPype node to connect the input to
        node_input : str
            Name of the input on the node to connect the dataset spec to
        """
        assert spec_name in self.input_names, (
            "'{}' is not a valid input for '{}' pipeline ('{}')"
            .format(spec_name, self.name, "', '".join(self._inputs)))
        self._workflow.connect(self._inputnode, spec_name, node, node_input)
        if spec_name in self._unconnected_inputs:
            self._unconnected_inputs.remove(spec_name)

    def connect_output(self, spec_name, node, node_output):
        """
        Connects an output to a study dataset spec

        Parameters
        ----------
        spec_name : str
            Name of the study dataset spec to connect to
        node : nipype.pipeline.BaseNode
            A NiPype to connect the output from
        node_output : str
            Name of the output on the node to connect to the dataset
        """
        assert spec_name in self.output_names, (
            "'{}' is not a valid output for '{}' pipeline ('{}')"
            .format(spec_name, self.name, "', '".join(self.output_names)))
        assert spec_name in self._unconnected_outputs, (
            "'{}' output has been connected already")
        outputnode = self._outputnodes[
            self._study.dataset_spec(spec_name).multiplicity]
        self._workflow.connect(node, node_output, outputnode, spec_name)
        self._unconnected_outputs.remove(spec_name)

    def connect_subject_id(self, node, node_input):
        """
        Connects the subject ID from the input node of the pipeline to an
        internal node

        Parameters
        ----------
        node : BaseNode
            The node to connect the subject ID to
        node_input : str
            The name of the field of the node to connect the subject ID to
        """
        self._workflow.connect(self._inputnode, 'subject_id', node, node_input)

    def connect_visit_id(self, node, node_input):
        """
        Connects the visit ID from the input node of the pipeline to an
        internal node

        Parameters
        ----------
        node : BaseNode
            The node to connect the subject ID to
        node_input : str
            The name of the field of the node to connect the subject ID to
        """
        self._workflow.connect(self._inputnode, 'visit_id', node, node_input)

    def create_node(self, interface, name, requirements=[],
                    wall_time=DEFAULT_WALL_TIME,
                    memory=DEFAULT_MEMORY, nthreads=1, gpu=False, **kwargs):
        """
        Creates a Node in the pipeline (prepending the pipeline namespace)

        Parameters
        ----------
        interface : nipype.Interface
            The interface to use for the node
        name : str
            Name for the node
        requirements : list(Requirement)
            List of required packages need for the node to run (default: [])
        wall_time : float
            Time required to execute the node in minutes (default: 1)
        memory : int
            Required memory for the node in MB (default: 1000)
        nthreads : int
            Preferred number of threads to run the node on (default: 1)
        gpu : bool
            Flags whether a GPU compute node is preferred or not
            (default: False)
        """
        node = Node(interface, name="{}_{}".format(self._name, name),
                    requirements=requirements, wall_time=wall_time,
                    nthreads=nthreads, memory=memory, gpu=gpu,
                    **kwargs)
        self._workflow.add_nodes([node])
        return node

    def create_map_node(self, interface, name, requirements=[],
                        wall_time=DEFAULT_WALL_TIME,
                        memory=DEFAULT_MEMORY, nthreads=1, gpu=False,
                        **kwargs):
        """
        Creates a MapNode in the pipeline (prepending the pipeline namespace)

        Parameters
        ----------
        interface : nipype.Interface
            The interface to use for the node
        name : str
            Name for the node
        requirements : list(Requirement)
            List of required packages need for the node to run (default: [])
        wall_time : float
            Time required to execute the node in minutes (default: 1)
        memory : int
            Required memory for the node in MB (default: 1000)
        nthreads : int
            Preferred number of threads to run the node on (default: 1)
        gpu : bool
            Flags whether a GPU compute node is preferred or not
            (default: False)
        """
        node = MapNode(interface, name="{}_{}".format(self._name, name),
                       requirements=requirements, wall_time=wall_time,
                       nthreads=nthreads, memory=memory, gpu=gpu,
                       **kwargs)
        self._workflow.add_nodes([node])
        return node

    def create_join_sessions_node(self, interface, joinfield, name,
                                  requirements=[], wall_time=DEFAULT_WALL_TIME,
                                  memory=DEFAULT_MEMORY,
                                  nthreads=1, gpu=False, **kwargs):
        """
        Creates a JoinNode that joins an input over all sessions (see
        nipype.readthedocs.io/en/latest/users/joinnode_and_itersource.html)

        Parameters
        ----------
        interface : nipype.Interface
            The interface to use for the node
        joinfield : str | list(str)
            The name of the field(s) to join into a list
        name : str
            Name for the node
        requirements : list(Requirement)
            List of required packages need for the node to run (default: [])
        wall_time : float
            Time required to execute the node in minutes (default: 1)
        memory : int
            Required memory for the node in MB (default: 1000)
        nthreads : int
            Preferred number of threads to run the node on (default: 1)
        gpu : bool
            Flags whether a GPU compute node is preferred or not
            (default: False)
        """
        node = JoinNode(interface,
                        joinsource='{}_sessions'.format(self.name),
                        joinfield=joinfield, name=name,
                        requirements=requirements, wall_time=wall_time,
                        nthreads=nthreads, memory=memory, gpu=gpu,
                        **kwargs)
        self._workflow.add_nodes([node])
        return node

    def create_join_subjects_node(self, interface, joinfield, name,
                                  requirements=[], wall_time=DEFAULT_WALL_TIME,
                                  memory=DEFAULT_MEMORY, nthreads=1, gpu=False,
                                  **kwargs):
        """
        Creates a JoinNode that joins an input over all sessions (see
        nipype.readthedocs.io/en/latest/users/joinnode_and_itersource.html)

        Parameters
        ----------
        interface : nipype.Interface
            The interface to use for the node
        joinfield : str | list(str)
            The name of the field(s) to join into a list
        name : str
            Name for the node
        requirements : list(Requirement)
            List of required packages need for the node to run (default: [])
        wall_time : float
            Time required to execute the node in minutes (default: 1)
        memory : int
            Required memory for the node in MB (default: 1000)
        nthreads : int
            Preferred number of threads to run the node on (default: 1)
        gpu : bool
            Flags whether a GPU compute node is preferred or not
            (default: False)
        """
        node = JoinNode(interface,
                        joinsource='{}_subjects'.format(self.name),
                        joinfield=joinfield, name=name,
                        requirements=requirements, wall_time=wall_time,
                        nthreads=nthreads, memory=memory, gpu=gpu, **kwargs)
        self._workflow.add_nodes([node])
        return node

    @property
    def name(self):
        return self._name

    @property
    def study(self):
        return self._study

    @property
    def workflow(self):
        return self._workflow

    @property
    def version(self):
        return self._version

    @property
    def inputs(self):
        return iter(self._inputs)

    @property
    def outputs(self):
        return chain(*self._outputs.values())

    @property
    def input_names(self):
        return (i.name for i in self.inputs)

    @property
    def output_names(self):
        return (o.name for o in self.outputs)

    @property
    def default_options(self):
        return self._default_options

    @property
    def options(self):
        return self._options

    def option(self, name):
        return self._options[name]

    @property
    def non_default_options(self):
        return ((k, v) for k, v in self.options.iteritems()
                if v != self.default_options[k])

    @property
    def description(self):
        return self._description

    @property
    def inputnode(self):
        return self._inputnode

    def outputnode(self, multiplicity):
        """
        Returns the output node for the given multiplicity

        Parameters
        ----------
        multiplicity : str
            One of 'per_session', 'per_subject', 'per_visit' and
            'per_project', specifying whether the dataset is present for each
            session, subject, visit or project.
        """
        return self._outputnodes[multiplicity]

    @property
    def mutliplicities(self):
        "The multiplicities present in the pipeline outputs"
        return self._outputs.iterkeys()

    def multiplicity_outputs(self, mult):
        return iter(self._outputs[mult])

    def multiplicity_output_names(self, mult):
        return (o.name for o in self.multiplicity_outputs(mult))

    def multiplicity(self, output):
        mults = [m for m, outputs in self._outputs.itervalues()
                 if output in outputs]
        if not mults:
            raise KeyError(
                "'{}' is not an output of pipeline '{}'".format(output,
                                                                self.name))
        else:
            assert len(mults) == 1
            mult = mults[0]
        return mult

    @property
    def citations(self):
        return self._citations

    def node(self, name):
        return self.workflow.get_node('{}_{}'.format(self.name, name))

    @property
    def suffix(self):
        """
        A suffixed appended to output filenames when they are archived to
        identify the options used to generate them
        """
        return '__'.join('{}_{}'.format(k, v)
                         for k, v in self.options.iteritems())

    def add_input(self, input_name):
        """
        Adds a new input to the pipeline. Useful if extending a pipeline in a
        derived Study class

        Parameters
        ----------
        input_name : str
            Name of the input to add to the pipeline
        """
        if input_name not in self.study.dataset_spec_names():
            raise NiAnalysisDatasetNameError(
                "'{}' is not a name of a dataset_spec in {} Studys"
                .format(input_name, self.study.name))
        self._inputs.append(input_name)

    def assert_connected(self):
        """
        Check for unconnected inputs and outputs after pipeline construction
        """
        assert not self._unconnected_inputs, (
            "'{}' input{} not connected".format(
                "', '".join(self._unconnected_inputs),
                ('s are' if len(self._unconnected_inputs) > 1 else ' is')))
        assert not self._unconnected_outputs, (
            "'{}' output{} not connected".format(
                "', '".join(self._unconnected_outputs),
                ('s are' if len(self._unconnected_outputs) > 1 else ' is')))
