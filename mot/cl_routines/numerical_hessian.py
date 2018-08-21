import itertools
import numpy as np
from mot.lib.cl_function import SimpleCLFunction
from mot.configuration import CLRuntimeInfo, config_context, CLRuntimeAction
from mot.lib.kernel_data import Array, Zeros
from scipy import linalg


__author__ = 'Robbert Harms'
__date__ = '2017-10-16'
__maintainer__ = 'Robbert Harms'
__email__ = 'robbert.harms@maastrichtuniversity.nl'
__licence__ = 'LGPL v3'


# todo: refactor out the model class
def numerical_hessian(model, objective_func,
                      parameters, lower_bounds, upper_bounds,
                      step_ratio=2, nmr_steps=15, data=None,
                      step_offset=None, cl_runtime_info=None):
    """Calculate and return the Hessian of the given function at the given parameters.

    This calculates the Hessian using central difference (using a 2nd order Taylor expansion) with a Richardson
    extrapolation over the proposed sequence of steps.

    The Hessian is evaluated at the steps:

    .. math::
        \quad  ((f(x + d_j e_j + d_k e_k) - f(x + d_j e_j - d_k e_k)) -
                (f(x - d_j e_j + d_k e_k) - f(x - d_j e_j - d_k e_k)) /
                (4 d_j d_k)

    where :math:`e_j` is a vector where element :math:`j` is one and the rest are zero
    and :math:`d_j` is a scalar spacing :math:`steps_j`.

    Steps are generated according to a exponentially diminishing ratio defined as:

        steps = max_step * step_ratio**-(i+offset), i=0, 1,.., nmr_steps-1.

    Where the max step is taken from the model. For example, a maximum step of 2 with a step ratio of 2 and with
    4 steps gives: [2.0, 1.0, 0.5, 0.25]. If offset would be 2, we would instead get: [0.5, 0.25, 0.125, 0.0625].

    If number of steps is 1, we use not Richardson extrapolation and return the results of the first step. If the
    number of steps is 2 we use a first order Richardson extrapolation step. For all higher number of steps
    we use a second order Richardson extrapolation.

    Args:
        model (mot.cl_routines.numerical_hessian.NumericalDerivativeInterface): the model to use for calculating the
            derivative.
        objective_func (mot.lib.cl_function.CLFunction): The function we want to differentiate.
            A CL function with the signature:

            .. code-block:: c

                double <func_name>(local const mot_float_type* const x,
                                   void* data,
                                   local mot_float_type* objective_list);

            The objective function has the same signature as the minimization function in MOT. For the numerical
            hessian, the ``objective_list`` parameter is ignored.

        parameters (ndarray): The parameters at which to evaluate the gradient. A (d, p) matrix with d problems,
            p parameters and n samples.
        lower_bounds (list): a list of length (p,) for p parameters with the lower bounds.
            Each element of the list can be a scalar or a vector (of the same length as the number of
            problem instances). For infinity use np.inf.
        upper_bounds (list): a list of length (p,) for p parameters with the upper bounds.
            Each element of the list can be a scalar or a vector (of the same length as the number of
            problem instances). For infinity use np.inf.
        step_ratio (float): the ratio at which the steps diminish.
        nmr_steps (int): the number of steps we will generate. We will calculate the derivative for each of these
            step sizes and extrapolate the best step size from among them. The minimum number of steps is 2.
        data (mot.lib.kernel_data.KernelData): the user provided data for the ``void* data`` pointer.
        step_offset (the offset in the steps, if set we start the steps from the given offset.
        cl_runtime_info (mot.configuration.CLRuntimeInfo): the runtime information

    Returns:
        ndarray: the gradients for each of the parameters for each of the problems
    """
    if len(parameters.shape) == 1:
        parameters = parameters[None, :]
    nmr_params = parameters.shape[1]

    parameter_scalings = np.array(model.numdiff_get_scaling_factors())

    def finalize_derivatives(derivatives):
        """Transforms the derivatives from vector to matrix and apply the parameter scalings."""
        return _results_vector_to_matrix(derivatives, nmr_params) * np.outer(parameter_scalings, parameter_scalings)

    with config_context(CLRuntimeAction(cl_runtime_info or CLRuntimeInfo())):
        derivatives = _compute_derivatives(model, objective_func, parameters, step_ratio, step_offset, nmr_steps,
                                           lower_bounds, upper_bounds, data=data)

        if nmr_steps == 1:
            return finalize_derivatives(derivatives[..., 0])

        derivatives, errors = _richardson_extrapolation(derivatives, step_ratio)

        if nmr_steps <= 3:
            return finalize_derivatives(derivatives[..., 0])

        if derivatives.shape[2] > 2:
            derivatives, errors = _wynn_extrapolate(derivatives)

        if derivatives.shape[2] == 1:
            return finalize_derivatives(derivatives[..., 0])

        derivatives, errors = _median_outlier_extrapolation(derivatives, errors)
        return finalize_derivatives(derivatives)


def _compute_derivatives(model, objective_func, parameters, step_ratio, step_offset, nmr_steps,
                         lower_bounds, upper_bounds, data=None):
    """Compute the lower triangular elements of the Hessian using the central difference method.

    This will compute the elements of the Hessian multiple times with decreasing step sizes.

    Args:
        model: the log likelihood model we are trying to differentiate
        parameters (ndarray): a (n, p) matrix with for for every problem n, p parameters. These are the points
            at which we want to calculate the derivative
        step_ratio (float): the ratio at which the steps exponentially diminish
        step_offset (int): ignore the first few step sizes by this offset
        nmr_steps (int): the number of steps to compute and return (after the step offset)
        lower_bounds (list): lower bounds
        upper_bounds (list): upper bounds
        data (mot.lib.kernel_data.KernelData): the user provided data for the ``void* data`` pointer.
    """
    parameter_scalings = np.array(model.numdiff_get_scaling_factors())

    nmr_params = parameters.shape[1]
    nmr_derivatives = (nmr_params ** 2 - nmr_params) // 2 + nmr_params

    initial_step = _get_initial_step_size(model, parameters, lower_bounds, upper_bounds)
    if step_offset:
        initial_step *= float(step_ratio) ** -step_offset

    kernel_data = {
        'data': data,
        'parameters': Array(parameters, ctype='mot_float_type'),
        'parameter_scalings_inv': Array(1. / parameter_scalings, ctype='float', offset_str='0'),
        'initial_step': Array(initial_step, ctype='float'),
        'step_evaluates': Zeros((parameters.shape[0], nmr_derivatives, nmr_steps), 'double'),
    }

    _derivation_kernel(model, objective_func, nmr_params, nmr_steps, step_ratio).evaluate(
        kernel_data, parameters.shape[0], use_local_reduction=True)

    return kernel_data['step_evaluates'].get_data()


def _richardson_extrapolation(derivatives, step_ratio):
    """Apply the Richardson extrapolation to the derivatives computed with different steps.

    Having for every problem instance and every Hessian element multiple derivatives computed with decreasing steps,
    we can now apply Richardson extrapolation to reduce the error term from :math:`\mathcal{O}(h^{2})` to
    :math:`\mathcal{O}(h^{4})` or :math:`\mathcal{O}(h^{6})` depending on how many steps we have calculated.

    This method only considers extrapolation up to the sixth error order. For a set of two derivatives we compute
    a single fourth order approximation, for three derivatives and up we compute ``n-2`` sixth order approximations.
    Expected errors for approximation ``i`` are computed using the ``i+1`` derivative plus a statistical error
    based on the machine precision.

    Args:
        derivatives (ndarray): (n, p, s), a matrix with for n problems and p parameters, s step sizes.
        step_ratio (ndarray): the diminishing ratio of the steps used to compute the derivatives.
    """
    nmr_problems, nmr_derivatives, nmr_steps = derivatives.shape
    richardson_coefficients = _get_richardson_coefficients(step_ratio, min(nmr_steps, 3) - 1)
    nmr_convolutions_needed = nmr_steps - (len(richardson_coefficients) - 2)
    final_nmr_convolutions = nmr_convolutions_needed - 1

    kernel_data = {
        'derivatives': Array(derivatives, 'double', offset_str='{problem_id} * ' + str(nmr_steps)),
        'richardson_extrapolations': Zeros(
            (nmr_problems * nmr_derivatives, nmr_convolutions_needed), 'double', mode='rw'),
        'errors': Zeros(
            (nmr_problems * nmr_derivatives, final_nmr_convolutions), 'double', mode='rw'),
    }

    richardson_func = _richardson_error_kernel(nmr_steps, nmr_convolutions_needed, richardson_coefficients)
    richardson_func.evaluate(kernel_data, nmr_problems * nmr_derivatives,
                             use_local_reduction=False)

    richardson_extrapolations = np.reshape(kernel_data['richardson_extrapolations'].get_data(),
                                           (nmr_problems, nmr_derivatives, nmr_convolutions_needed))
    errors = np.reshape(kernel_data['errors'].get_data(),
                        (nmr_problems, nmr_derivatives, final_nmr_convolutions))

    return richardson_extrapolations[..., :final_nmr_convolutions], errors


def _wynn_extrapolate(derivatives):
    nmr_problems, nmr_derivatives, nmr_steps = derivatives.shape
    nmr_extrapolations = nmr_steps - 2

    kernel_data = {
        'derivatives': Array(derivatives, 'double', offset_str='{problem_id} * ' + str(nmr_steps)),
        'extrapolations': Zeros((nmr_problems * nmr_derivatives, nmr_extrapolations), 'double', mode='rw'),
        'errors': Zeros((nmr_problems * nmr_derivatives, nmr_extrapolations), 'double', mode='rw'),
    }

    wynn_func = _wynn_extrapolation_kernel(nmr_steps)
    wynn_func.evaluate(kernel_data, nmr_problems * nmr_derivatives,
                       use_local_reduction=False)

    extrapolations = np.reshape(kernel_data['extrapolations'].get_data(),
                                (nmr_problems, nmr_derivatives, nmr_extrapolations))
    errors = np.reshape(kernel_data['errors'].get_data(), (nmr_problems, nmr_derivatives, nmr_extrapolations))
    return extrapolations, errors


def _median_outlier_extrapolation(derivatives, errors):
    """Add an error to outliers and afterwards return the derivatives with the lowest errors.

    This seems to be the slowest function in the library. Perhaps one day update this to OpenCL as well.
    Some ideas are in: http://krstn.eu/np.nanpercentile()-there-has-to-be-a-faster-way/
    """
    def _get_median_outliers_errors(der, trim_fact=10):
        """Discards any estimate that differs wildly from the median of the estimates (of that derivative).

        A factor of 10 to 1 in either direction . The actual trimming factor is
        defined as a parameter.
        """
        p25, median, p75 = np.nanpercentile(der, q=[25, 50, 75], axis=2)[..., None]
        iqr = np.abs(p75 - p25)

        a_median = np.abs(median)
        outliers = (((abs(der) < (a_median / trim_fact)) +
                     (abs(der) > (a_median * trim_fact))) * (a_median > 1e-8) +
                    ((der < p25 - 1.5 * iqr) + (p75 + 1.5 * iqr < der)))
        return outliers * np.abs(der - median)

    all_nan = np.where(np.sum(np.isnan(errors), axis=2) == errors.shape[2])
    errors[all_nan[0], all_nan[1], 0] = 0
    derivatives[all_nan[0], all_nan[1], 0] = 0

    errors += _get_median_outliers_errors(derivatives)

    minpos = np.nanargmin(errors, axis=2)
    indices = np.indices(minpos.shape)

    derivatives_final = derivatives[indices[0], indices[1], minpos]
    errors_final = errors[indices[0], indices[1], minpos]

    derivatives_final[all_nan[0], all_nan[1]] = np.nan
    errors_final[all_nan[0], all_nan[1]] = np.nan

    return derivatives_final, errors_final


def _results_vector_to_matrix(vectors, nmr_params):
    """Transform for every problem (and optionally every step size) the vector results to a square matrix.

    Since we only process the lower triangular items, we have to convert the 1d vectors into 2d matrices for every
    problem. This function does that.

    Args:
        vectors (ndarray): for every problem (and step size) the 1d vector with results
        nmr_params (int): the number of parameters
    """
    matrices = np.zeros(vectors.shape[:-1] + (nmr_params, nmr_params), dtype=vectors.dtype)

    ltr_ind = 0
    for px in range(nmr_params):
        matrices[..., px, px] = vectors[..., ltr_ind]
        ltr_ind += 1

        for py in range(px + 1, nmr_params):
            matrices[..., px, py] = vectors[..., ltr_ind]
            matrices[..., py, px] = vectors[..., ltr_ind]
            ltr_ind += 1
    return matrices


def _get_initial_step_size(model, parameters, lower_bounds, upper_bounds):
    """Get an initial step size to use for every parameter.

    This chooses the maximum of the maximum step defined in the model and the minimum step possible
    for a parameter given its bounds.

    Args:
        model (mot.cl_routines.numerical_hessian.NumericalDerivativeInterface): the model to use for calculating the
            derivative.
        parameters (ndarray): The parameters at which to evaluate the gradient. A (d, p) matrix with d problems,
            p parameters and n samples.

    Returns:
        ndarray: for every problem instance the vector with the initial step size for each parameter.
    """
    max_step = model.numdiff_get_max_step()

    initial_step = np.zeros_like(parameters)

    for ind in range(parameters.shape[1]):
        if model.numdiff_use_bounds()[ind]:
            use_lower = model.numdiff_use_lower_bounds()[ind]
            use_upper = model.numdiff_use_upper_bounds()[ind]

            if use_upper and not use_lower:
                minimum_allowed_step = np.abs(upper_bounds[ind] - parameters[:, ind]) \
                                       * model.numdiff_get_scaling_factors()[ind]
            elif use_lower and not use_upper:
                minimum_allowed_step = np.abs(parameters[:, ind] - lower_bounds[ind]) \
                                       * model.numdiff_get_scaling_factors()[ind]
            else:
                minimum_allowed_step = np.minimum(np.abs(parameters[:, ind] - lower_bounds[ind]),
                                                  np.abs(upper_bounds[ind] - parameters[:, ind])) \
                                       * model.numdiff_get_scaling_factors()[ind]
            initial_step[:, ind] = np.minimum(minimum_allowed_step, max_step[ind])
        else:
            initial_step[:, ind] = max_step[ind]

    return initial_step


def _get_richardson_coefficients(step_ratio, nmr_extrapolations):
    """Get the matrix with the convolution rules.

    In the kernel we will extrapolate the sequence based on the Richardsons method.

    This method assumes we have a series expansion like:

        L = f(h) + a0 * h^p_0 + a1 * h^p_1+ a2 * h^p_2 + ...

    where p_i = order + step * i  and f(h) -> L as h -> 0, but f(0) != L.

    If we evaluate the right hand side for different stepsizes h we can fit a polynomial to that sequence
    of approximations.

    Instead of using all the generated points at the same time, we convolute the Richardson method over the
    acquired steps to approximate the higher order error terms.

    Args:
        step_ratio (float): the ratio at which the steps diminish.
        nmr_extrapolations (int): the number of extrapolations we want to do. Each extrapolation requires
            an evaluation at an exponentially decreasing step size.

    Returns:
        ndarray: a vector with the extrapolation coefficients.
    """
    if nmr_extrapolations == 0:
        return np.array([1])

    error_diminishing_per_step = 2
    taylor_expansion_order = 2

    def r_matrix(num_terms):
        i, j = np.ogrid[0:num_terms + 1, 0:num_terms]
        r_mat = np.ones((num_terms + 1, num_terms + 1))
        r_mat[:, 1:] = (1.0 / step_ratio) ** (i * (error_diminishing_per_step * j + taylor_expansion_order))
        return r_mat

    return linalg.pinv(r_matrix(nmr_extrapolations))[0]


def _get_compute_functions_cl(model, objective_func, nmr_params, nmr_steps, step_ratio):
    numdiff_param_transform = model.numdiff_parameter_transformation()

    func = objective_func.get_cl_code()
    func += numdiff_param_transform.get_cl_code()

    return func + '''
        double _calculate_function(void* data, local mot_float_type* x){
            return ''' + objective_func.get_cl_function_name() + '''(x, data, 0);
        }
        
        /**
         * Evaluate the model with a perturbation in two dimensions.
         *
         * Args:
         *  data: the data container
         *  x_input: the array with the input parameters
         *  perturb_dim_0: the index (into the x_input parameters) of the first parameter to perturbate
         *  perturb_0: the added perturbation of the index corresponding to ``perturb_dim_0``
         *  perturb_dim_1: the index (into the x_input parameters) of the second parameter to perturbate
         *  perturb_1: the added perturbation of the index corresponding to ``perturb_dim_1``
         *
         * Returns:
         *  the function evaluated at the parameters plus their perturbation.
         */
        double _eval_step(void* data, local mot_float_type* x_input,
                          uint perturb_dim_0, mot_float_type perturb_0,
                          uint perturb_dim_1, mot_float_type perturb_1){

            local mot_float_type x_tmp[''' + str(nmr_params) + '''];
            
            if(get_local_id(0) == 0){
                for(uint i = 0; i < ''' + str(nmr_params) + '''; i++){
                    x_tmp[i] = x_input[i];
                }
                x_tmp[perturb_dim_0] += perturb_0;
                x_tmp[perturb_dim_1] += perturb_1;
            }
            barrier(CLK_LOCAL_MEM_FENCE);

            ''' + numdiff_param_transform.get_cl_function_name() + '''(data, x_tmp);
            return _calculate_function(data, x_tmp);
        }
        
        /**
         * Compute one element of the Hessian for a number of steps.
         * 
         * This uses the initial steps in the data structure, indexed by the parameters to change (px, py).
         */
        void _compute_steps(void* data, local mot_float_type* x_input, mot_float_type f_x_input,
                            uint px, uint py, global double* step_evaluates, 
                            global float* parameter_scalings_inv,
                            global float* initial_step){
            
            double step_x;
            double step_y;
            double tmp;
            bool is_first_workitem = get_local_id(0) == 0;
            
            if(px == py){
                for(uint step_ind = 0; step_ind < ''' + str(nmr_steps) + '''; step_ind++){
                    step_x = initial_step[px] / pown(''' + str(float(step_ratio)) + ''', step_ind);
                    
                    tmp = (
                          _eval_step(data, x_input,
                                     px, 2 * (step_x * parameter_scalings_inv[px]),
                                     0, 0)
                        + _eval_step(data, x_input,
                                     px, -2 * (step_x * parameter_scalings_inv[px]),
                                     0, 0)
                        - 2 * f_x_input
                    ) / (4 * step_x * step_x);
                    
                    if(is_first_workitem){
                        step_evaluates[step_ind] = tmp;
                    }
                }
            }
            else{
                for(uint step_ind = 0; step_ind < ''' + str(nmr_steps) + '''; step_ind++){
                    step_x = initial_step[px] / pown(''' + str(float(step_ratio)) + ''', step_ind);
                    step_y = initial_step[py] / pown(''' + str(float(step_ratio)) + ''', step_ind);
                    
                    tmp = (
                          _eval_step(data, x_input,
                                     px, step_x * parameter_scalings_inv[px],
                                     py, step_y * parameter_scalings_inv[py])
                        - _eval_step(data, x_input,
                                     px, step_x * parameter_scalings_inv[px],
                                     py, -step_y * parameter_scalings_inv[py])
                        - _eval_step(data, x_input,
                                     px, -step_x * parameter_scalings_inv[px],
                                     py, step_y * parameter_scalings_inv[py])
                        + _eval_step(data, x_input,
                                     px, -step_x * parameter_scalings_inv[px],
                                     py, -step_y * parameter_scalings_inv[py])
                    ) / (4 * step_x * step_y);

                    if(is_first_workitem){
                        step_evaluates[step_ind] = tmp;
                    }                       
                }
            }
        }
    '''


def _get_error_estimate_functions_cl(nmr_steps, nmr_convolutions,
                                     richardson_coefficients):
    func = '''
        /**
         * Apply a simple kernel convolution over the results from each row of steps.
         *
         * This applies a convolution starting from the starting step index that contained a valid move.
         * It uses the mode 'reflect' to deal with outside points.
         *
         * Please note that this kernel is hard coded to work with 2nd order Taylor expansions derivatives only.

         * Args:
         *  step_evaluates: the step evaluates for each row of the step sizes
         *  richardson_extrapolations: the array to place the convoluted results in
         */
        void _apply_richardson_convolution(
                global double* derivatives,
                global double* richardson_extrapolations){

            double convolution_kernel[''' + str(len(richardson_coefficients)) + '''] = {''' + \
                ', '.join(map(str, richardson_coefficients)) + '''};
            
            for(uint step_ind = 0; step_ind < ''' + str(nmr_convolutions) + '''; step_ind++){
                
                // convolute the Richardson coefficients
                for(uint kernel_ind = 0; kernel_ind < ''' + str(len(richardson_coefficients)) + '''; kernel_ind++){
                    
                    uint kernel_step_ind = step_ind + kernel_ind;

                    // reflect
                    if(kernel_step_ind >= ''' + str(nmr_steps) + '''){
                        kernel_step_ind -= 2 * (kernel_step_ind - ''' + str(nmr_steps) + ''') + 1;
                    }

                    richardson_extrapolations[step_ind] +=
                        derivatives[kernel_step_ind] * convolution_kernel[kernel_ind];
                }
            }
        }
        
        /**
         * Compute the errors from using the Richardson extrapolation.
         *
         * "A neat trick to compute the statistical uncertainty in the estimate of our desired derivative is to use 
         *  statistical methodology for that error estimate. While I do appreciate that there is nothing truly 
         *  statistical or stochastic in this estimate, the approach still works nicely, providing a very 
         *  reasonable estimate in practice. A three term Richardson-like extrapolant, then evaluated at four 
         *  distinct values for \delta, will yield an estimate of the standard error of the constant term, with one 
         *  spare degree of freedom. The uncertainty is then derived by multiplying that standard error by the 
         *  appropriate percentile from the Students-t distribution." 
         * 
         * Cited from https://numdifftools.readthedocs.io/en/latest/src/numerical/derivest.html
         * 
         * In addition to the error derived from the various estimates, we also approximate the numerical round-off 
         * errors in this method. All in all, the resulting errors should reflect the absolute error of the
         * estimates.
         */ 
        void _compute_richardson_errors(
                global double* derivatives, 
                global double* richardson_extrapolations,
                global double* errors){
            
            // The magic number 12.7062... follows from the student T distribution with one dof. 
            //  >>> import scipy.stats as ss
            //  >>> allclose(ss.t.cdf(12.7062047361747, 1), 0.975) # True
            double fact = max(
                (mot_float_type)''' + str(12.7062047361747 * np.sqrt(np.sum(richardson_coefficients**2))) + ''',
                (mot_float_type)MOT_EPSILON * 10);
            
            double tolerance;
            double error;
            
            for(uint conv_ind = 0; conv_ind < ''' + str(nmr_convolutions - 1) + '''; conv_ind++){
                tolerance = max(fabs(richardson_extrapolations[conv_ind + 1]), 
                                fabs(richardson_extrapolations[conv_ind])
                                ) * MOT_EPSILON * fact;
                
                error = fabs(richardson_extrapolations[conv_ind] - richardson_extrapolations[conv_ind + 1]) * fact;
                
                if(error <= tolerance){
                    error += tolerance * 10;
                }
                else{
                    error += fabs(richardson_extrapolations[conv_ind] - 
                                  derivatives[''' + str(nmr_steps - nmr_convolutions + 1) + ''' + conv_ind]) * fact;
                }
                
                errors[conv_ind] = error;
            }
        }
    '''
    return func


def _derivation_kernel(model, objective_func, nmr_params, nmr_steps, step_ratio):
    coords = [(x, y) for x, y in itertools.combinations_with_replacement(range(nmr_params), 2)]
    func = _get_compute_functions_cl(model, objective_func, nmr_params, nmr_steps, step_ratio)

    return SimpleCLFunction.from_string('''
        void compute(local mot_float_type* parameters,
                     global float* parameter_scalings_inv,
                     global float* initial_step,
                     global double* step_evaluates,
                     void* data){
                                 
            double f_x_input = _calculate_function(data, parameters);
            
            uint coords[''' + str(len(coords)) + '''][2] = {
                ''' + ', '.join('{{{}, {}}}'.format(*c) for c in coords)  + '''
            };
            
            for(uint coord_ind = 0; coord_ind < ''' + str(len(coords)) + '''; coord_ind++){
                _compute_steps(data, parameters, f_x_input, coords[coord_ind][0], coords[coord_ind][1], 
                               step_evaluates + coord_ind * ''' + str(nmr_steps) + ''', parameter_scalings_inv,
                               initial_step);
            }
        }
    ''', cl_extra=func)


def _richardson_error_kernel(nmr_steps, nmr_convolutions, richardson_coefficients):
    func = _get_error_estimate_functions_cl(nmr_steps, nmr_convolutions, richardson_coefficients)

    return SimpleCLFunction.from_string('''
        void convolute(global double* derivatives, global double* richardson_extrapolations, global double* errors){
            _apply_richardson_convolution(derivatives, richardson_extrapolations);
            _compute_richardson_errors(derivatives, richardson_extrapolations, errors);
        }
    ''', cl_extra=func)


def _wynn_extrapolation_kernel(nmr_steps):
    """OpenCL kernel for extrapolating a slowly convergent sequence.

    This algorithm, known in the Python Numdifftools as DEA3, attempts to extrapolate nonlinearly to a better
    estimate of the sequence's limiting value, thus improving the rate of convergence. The routine is based on the
    epsilon algorithm of P. Wynn [1].

    References:
    - [1] C. Brezinski and M. Redivo Zaglia (1991)
            "Extrapolation Methods. Theory and Practice", North-Holland.

    - [2] C. Brezinski (1977)
            "Acceleration de la convergence en analyse numerique",
            "Lecture Notes in Math.", vol. 584,
            Springer-Verlag, New York, 1977.

    - [3] E. J. Weniger (1989)
            "Nonlinear sequence transformations for the acceleration of
            convergence and the summation of divergent series"
            Computer Physics Reports Vol. 10, 189 - 371
            http://arxiv.org/abs/math/0306302v1
    """
    return SimpleCLFunction.from_string('''
        void compute(global double* derivatives, global double* extrapolations, global double* errors){
            double v0, v1, v2; 
            
            double delta0, delta1; 
            double err0, err1; 
            double tol0, tol1; 
            
            double ss;
            bool converged;
            
            double result;
            
            for(uint i = 0; i < ''' + str(nmr_steps - 2) + '''; i++){
                v0 = derivatives[i];
                v1 = derivatives[i + 1];
                v2 = derivatives[i + 2];
                
                delta0 = v1 - v0;
                delta1 = v2 - v1;
                
                err0 = fabs(delta0);
                err1 = fabs(delta1);
                
                tol0 = max(fabs(v0), fabs(v1)) * MOT_EPSILON;
                tol1 = max(fabs(v1), fabs(v2)) * MOT_EPSILON;
                
                // avoid division by zero and overflow
                if(err0 < MOT_MIN){
                    delta0 = MOT_MIN;
                }
                if(err1 < MOT_MIN){
                    delta1 = MOT_MIN;
                }
                
                ss = 1.0 / delta1 - 1.0 / delta0 + MOT_MIN;
                converged = ((err0 <= tol0) && (err1 <= tol1)) || (fabs(ss * v1) <= 1e-3);
                
                result = (converged ? v2 : v1 + 1.0/ss);
                
                extrapolations[i] = result;
                errors[i] = err0 + err1 + (converged ? tol1 * 10 : fabs(result - v2));
            }
        }
    ''')


class NumericalDerivativeInterface(object):
    """Extends an optimization model for calculating numerical derivatives of the objective function.

    For calculating derivatives (gradients / Hessians) numerically, we need a likelihood function and some additional
    information, like the step size for each parameter, a method for checking boundary conditions and possible parameter
    transformations for circular parameters. All these extra elements are represented in this interface.
    """

    def numdiff_get_max_step(self):
        """Get for each estimable parameter the maximum step size to use for calculating numerical derivatives.

        The derivative calculation method typically uses an adaptive step size to determine the step with the best
        trade-off between numerical errors and localization of the derivative. This method must return the
        initial and largest step size to use.

        Returns:
            list[float]: per parameter a single float with the step size for that parameter
        """
        raise NotImplementedError()

    def numdiff_get_scaling_factors(self):
        """Get for each estimable parameter a scaling factor that is to be used for scaling this parameter to unitary.

        Since numerical differentiation is sensitive to differences in step sizes, it is better to rescale
        the parameters to a unitary range instead of changing the step sizes for the parameters.

        This should return numbers such that when the parameter is multiplied with this value, the magnitude of the
        parameter is about one.

        Returns:
            list[float]: per estimable parameter a single float with the parameter scaling to use for that parameter.
                Use 1 as identity.
        """
        raise NotImplementedError()

    def numdiff_use_bounds(self):
        """Check for each parameter if we should be using the bounds for that parameter when taking the derivative.

        Returns:
            list[bool]: per parameter a boolean to identify if we should use the bounds for that parameter.
        """
        raise NotImplementedError()

    def numdiff_use_upper_bounds(self):
        """Check for each parameter if we should be using the upper bounds when taking the derivative.

        This is only used if use_bounds is True for a parameter.

        Returns:
            list[bool]: per parameter a boolean to identify if we should use the upper bounds for that parameter.
        """
        raise NotImplementedError()

    def numdiff_use_lower_bounds(self):
        """Check for each parameter if we should be using the lower bounds when taking the derivative.

        This is only used if use_bounds is True for a parameter.

        Returns:
            list[bool]: per parameter a boolean to identify if we should use the lower bounds for that parameter.
        """
        raise NotImplementedError()

    def numdiff_parameter_transformation(self):
        """A transformation that can prepare the parameter +/- the proposed step for evaluation in the model function.

        Some parameters require for example a modulus operation before the proposed step can be used in the model
        This is the place to define them. Please note that this function does not need to incorporate boundary checks
        as those are handled already by the numerical differentiation routine.

        Returns:
            mot.lib.cl_function.CLFunction: A function with the signature:
                .. code-block:: c

                    void <func_name>(void* data, local mot_float_type* params);

                Where the data is the kernel data struct and params is the vector with the suggested parameters and
                which can be modified in place. Note that this is called two times, one with the parameters plus
                the step and one time without.
        """
        raise NotImplementedError()
