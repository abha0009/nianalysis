from itertools import chain
from nianalysis.study.base import set_dataset_specs
from nianalysis.dataset import DatasetSpec
from nianalysis.data_formats import nifti_gz_format
from ..base import MRStudy


class T2Study(MRStudy):

    def brain_mask_pipeline(self, robust=True, threshold=0.5,
                            reduce_bias=False, **kwargs):
        return super(T2Study, self).brain_mask_pipeline(
            robust=robust, threshold=threshold, reduce_bias=reduce_bias,
            **kwargs)

    _dataset_specs = set_dataset_specs(
        DatasetSpec('manual_wmh_mask', nifti_gz_format),
        inherit_from=chain(MRStudy.dataset_specs()))