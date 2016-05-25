
class FileFormat(object):

    def __init__(self, name, extension):
        self._name = name
        self._extension = extension

    def __repr__(self):
        return "FileFormat(name='{}')".format(self.name)

    @property
    def name(self):
        return self._name

    @property
    def extension(self):
        return self._extension


nifti_format = FileFormat(name='nifti', extension='nii')

nifti_gz_format = FileFormat(name='nifti_gz', extension='nii.gz')

mrtrix_format = FileFormat(name='mrtrix', extension='mif')

analyze_format = FileFormat(name='analyze', extension='img')

dicom_format = FileFormat(name='dicom', extension='')

bvecs_format = FileFormat(name='bvecs', extension='')

bvals_format = FileFormat(name='bvals', extension='')

mrtrix_grad_format = FileFormat(name='mrtrix_grad', extension='b')