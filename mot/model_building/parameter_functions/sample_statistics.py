import numpy as np

__author__ = 'Robbert Harms'
__date__ = "2014-10-23"
__license__ = "LGPL v3"
__maintainer__ = "Robbert Harms"
__email__ = "robbert.harms@maastrichtuniversity.nl"


class ParameterSampleStatistics(object):

    def get_point_estimate(self, samples):
        """Get the map that would represent the point estimate of these samples.

        This map is used for comparison with the point estimates obtained from optimization and typically corresponds
        to the mean of the distribution.

        Args:
            samples (ndarray): The 2d array with the samples per voxel.

        Returns:
            ndarray: The point estimate for every voxel.
        """
        raise NotImplementedError()

    def get_additional_statistics(self, samples):
        """Get additional statistics about the parameter distribution.

        This normally returns only a dictionary with a standard deviation map, but it can return more statistics
        if desired.

        Args:
            samples (ndarray): The 2d array with the samples per voxel.

        Returns:
            dict: dictionary with additional statistics. Example: ``{'std': ...}``
        """
        raise NotImplementedError()


class GaussianPSS(ParameterSampleStatistics):

    def get_point_estimate(self, samples):
        return np.mean(samples, axis=1)

    def get_additional_statistics(self, samples):
        return {'std': np.std(samples, axis=1)}


class CircularGaussianPSS(ParameterSampleStatistics):

    def __init__(self, max_angle=np.pi):
        """Compute the circular mean for samples in a range

        The minimum angle is set to 0, the maximum angle can be given.

        Args:
            max_angle (number): The maximum angle used in the calculations
        """
        super(CircularGaussianPSS, self).__init__()
        self.max_angle = max_angle

    def get_point_estimate(self, samples):
        return CircularGaussianPSS.circmean(np.mod(samples, self.max_angle), high=self.max_angle, low=0, axis=1)

    def get_additional_statistics(self, samples):
        return {'std': CircularGaussianPSS.circstd(np.mod(samples, self.max_angle), high=self.max_angle, low=0, axis=1)}

    @staticmethod
    def circmean(samples, high=2 * np.pi, low=0, axis=None):
        """Compute the circular mean for samples in a range.
        Taken from scipy.stats

        Args:
            samples (array_like): Input array.
            high (float or int): High boundary for circular mean range.  Default is ``2*pi``.
            low (float or int): Low boundary for circular mean range.  Default is 0.
            axis (int, optional): Axis along which means are computed.
                The default is to compute the mean of the flattened array.

        Returns:
            float: Circular mean.
        """
        ang = (samples - low) * 2 * np.pi / (high - low)
        res = np.angle(np.mean(np.exp(1j * ang), axis=axis))
        mask = res < 0
        if mask.ndim > 0:
            res[mask] += 2 * np.pi
        elif mask:
            res += 2 * np.pi
        return res * (high - low) / 2.0 / np.pi + low

    @staticmethod
    def circstd(samples, high=2 * np.pi, low=0, axis=None):
        """Compute the circular standard deviation for samples assumed to be in the range [low to high].

        Taken from scipy.stats, with a small change on the 4th line.

        This uses a definition of circular standard deviation that in the limit of
        small angles returns a number close to the 'linear' standard deviation.

        Args:
            samples (array_like): Input array.
            low (float or int): Low boundary for circular standard deviation range.  Default is 0.
            high (float or int): High boundary for circular standard deviation range. Default is ``2*pi``.
            axis (int): Axis along which standard deviations are computed.  The default is
                to compute the standard deviation of the flattened array.

        Returns:
            float: Circular standard deviation.
        """
        ang = (samples - low) * 2 * np.pi / (high - low)
        res = np.mean(np.exp(1j * ang), axis=axis)
        R = abs(res)
        R[R >= 1] = 1 - np.finfo(np.float).eps
        return ((high - low) / 2.0 / np.pi) * np.sqrt(-2 * np.log(R))
