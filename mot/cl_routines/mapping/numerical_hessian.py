import multiprocessing
import numpy as np
import os
from mot.cl_routines.mapping.run_procedure import RunProcedure
from ...utils import KernelInputArray, SimpleNamedCLFunction, KernelInputLocalMemory, KernelInputAllocatedOutput
from ...cl_routines.base import CLRoutine
from scipy import linalg
import warnings


__author__ = 'Robbert Harms'
__date__ = '2017-10-16'
__maintainer__ = 'Robbert Harms'
__email__ = 'robbert.harms@maastrichtuniversity.nl'
__licence__ = 'LGPL v3'


class NumericalHessian(CLRoutine):

    def calculate(self, model, parameters, codec=None, double_precision=False, nmr_steps=15, step_ratio=2):
        """Calculate and return the Hessian of the given function at the given parameters.

        This calculates the Hessian using a central difference at a 2nd order Taylor expansion.

        .. math::
            \quad  ((f(x + d_j e_j + d_k e_k) - f(x + d_j e_j - d_k e_k)) -
                    (f(x - d_j e_j + d_k e_k) - f(x - d_j e_j - d_k e_k)) /
                    (4 d_j d_k)

        where :math:`e_j` is a vector where element :math:`j` is one and the rest are zero
        and :math:`d_j` is a scalar spacing :math:`steps_j`.

        This method evaluates the Hessian at various step sizes and uses the Richardson extrapolation method
        to approximate the final Hessian.

        Steps are generated according to a exponentially diminishing ratio defined as:

            steps = max_step * step_ratio**-i, i=0, 1,.., nmr_steps-1.

        Where the max step is taken from the model. For example, a maximum step of 2 with a step ratio of 2 with 4 steps
        gives: [2.0, 1.0, 0.5, 0.25].

        Args:
            model (mot.model_interfaces.NumericalDerivativeInterface): the model to use for calculating the
                derivative.
            parameters (ndarray): The parameters at which to evaluate the gradient. A (d, p) matrix with d problems,
                p parameters and n samples.
            codec (mot.model_building.utils.ParameterCodec): a parameter codec used to constrain the parameters
                within bounds.
            double_precision (boolean): if we want to calculate everything in double precision
            nmr_steps (int): the number of smaller and smaller step sizes we will generate. We will
                calculate the derivative for each of these step sizes and extrapolate the best step size
                from among them.
            step_ratio (float): the ratio at which the steps diminish.

        Returns:
            ndarray: the gradients for each of the parameters for each of the problems
        """
        if len(parameters.shape) == 1:
            parameters = parameters[None, :]
        nmr_params = parameters.shape[1]

        parameter_scalings = np.array(model.numdiff_get_scaling_factors())

        elements_needed = (nmr_params**2 - nmr_params) // 2 + nmr_params
        steps = np.zeros((parameters.shape[0], nmr_steps, nmr_params))

        all_kernel_data = dict(model.get_kernel_data())
        all_kernel_data.update({
            'parameters': KernelInputArray(parameters, ctype='mot_float_type'),
            'local_reduction_lls': KernelInputLocalMemory('double'),
            'parameter_scalings': KernelInputArray(parameter_scalings, ctype='double', offset_str='0'),
            'steps': KernelInputArray(steps, ctype='mot_float_type', is_writable=True),
            'step_evaluates': KernelInputAllocatedOutput(
                (parameters.shape[0], nmr_steps, elements_needed), 'double'),
            'step_evaluates_convoluted': KernelInputAllocatedOutput(
                (parameters.shape[0], nmr_steps, elements_needed), 'double')
        })

        import time
        start = time.time()
        runner = RunProcedure(**self.get_cl_routine_kwargs())
        runner.run_procedure(self._get_wrapped_function(
            model, parameters, codec, step_ratio, model.numdiff_get_max_step(), nmr_steps),
            all_kernel_data, parameters.shape[0], double_precision=double_precision,
            use_local_reduction=True)

        print(time.time() - start)

        steps = all_kernel_data['steps'].get_data()
        step_evaluates = all_kernel_data['step_evaluates'].get_data()
        step_evaluates_convoluted = all_kernel_data['step_evaluates_convoluted'].get_data()

        full_step_evaluates = np.zeros((parameters.shape[0], nmr_steps, nmr_params, nmr_params), dtype=np.float64)
        full_step_evaluates_convoluted = np.zeros((parameters.shape[0], nmr_steps, nmr_params, nmr_params), dtype=np.float64)

        ltr_ind = 0
        for px in range(nmr_params):
            full_step_evaluates[..., px, px] = step_evaluates[..., ltr_ind]
            full_step_evaluates_convoluted[..., px, px] = step_evaluates_convoluted[..., ltr_ind]
            ltr_ind += 1

            for py in range(px + 1, nmr_params):
                full_step_evaluates[..., px, py] = step_evaluates[..., ltr_ind]
                full_step_evaluates_convoluted[..., px, py] = step_evaluates_convoluted[..., ltr_ind]

                full_step_evaluates[..., py, px] = step_evaluates[..., ltr_ind]
                full_step_evaluates_convoluted[..., py, px] = step_evaluates_convoluted[..., ltr_ind]

                ltr_ind += 1

        extrapolate = Extrapolate(step_ratio)

        # print(steps)

        def data_iterator():
            for param_ind in range(parameters.shape[0]):
                yield (full_step_evaluates[param_ind],
                       full_step_evaluates_convoluted[param_ind],
                       steps[param_ind],
                       nmr_params)

        # result = np.array(list(map(extrapolate, data_iterator())))

        if os.name == 'nt':  # In Windows there is no fork.
            result = np.array(list(map(extrapolate, data_iterator())))
        else:
            try:
                p = multiprocessing.Pool()
                result = np.array(list(p.imap(extrapolate, data_iterator())))
                p.close()
                p.join()
            except OSError:
                result = np.array(list(map(extrapolate, data_iterator())))

        return result * np.outer(parameter_scalings, parameter_scalings)

    def _get_richardson_rules(self, step_ratio):
        """Get the matrix with the convolution rules.

        In the kernel we will extrapolate the sequence based on the Richardsons method.

        This method assumes we have a series expansion like:

            L = f(h) + a0 * h^p_0 + a1 * h^p_1+ a2 * h^p_2 + ...

        where p_i = order + step * i  and f(h) -> L as h -> 0, but f(0) != L.

        If we evaluate the right hand side for different stepsizes h we can fit a polynomial to that sequence
        of approximations.

        Instead of using all the generated points at the same time, we convolute the Richardson method over the
        acquired steps to be able to approximate the errors.

        Args:
            step_ratio (float): the ratio at which the steps diminish.
        """
        step = 2
        taylor_expansion_order = 2

        def r_matrix(step, num_terms):
            i, j = np.ogrid[0:num_terms + 1, 0:num_terms]
            r_mat = np.ones((num_terms + 1, num_terms + 1))
            r_mat[:, 1:] = (1.0 / step_ratio) ** (i * (step * j + taylor_expansion_order))
            return r_mat

        return [linalg.pinv(r_matrix(step, seq_length))[0] for seq_length in range(1, 3)]

    def _get_wrapped_function(self, model, parameters, codec, step_ratio, max_steps, nmr_steps):
        ll_function = model.get_objective_per_observation_function()
        bound_check_function = model.numdiff_get_bound_check_function()
        nmr_inst_per_problem = model.get_nmr_inst_per_problem()

        nmr_params = parameters.shape[1]
        elements_needed = (nmr_params ** 2 - nmr_params) // 2 + nmr_params
        richardson_rules = self._get_richardson_rules(step_ratio)

        func = ''

        if codec is not None:
            func += codec.get_parameter_decode_function(function_name='decode')
            func += codec.get_parameter_encode_function(function_name='encode')

        func += ll_function.get_cl_code()
        func += bound_check_function.get_cl_code()

        func += '''
            double _calculate_function(mot_data_struct* data, mot_float_type* x){
                ulong observation_ind;
                ulong local_id = get_local_id(0);
                data->local_reduction_lls[local_id] = 0;
                uint workgroup_size = get_local_size(0);
                uint elements_for_workitem = ceil(''' + str(nmr_inst_per_problem) + ''' 
                                                  / (mot_float_type)workgroup_size);
                
                if(workgroup_size * (elements_for_workitem - 1) + local_id >= ''' + str(nmr_inst_per_problem) + '''){
                    elements_for_workitem -= 1;
                }
                
                for(uint i = 0; i < elements_for_workitem; i++){
                    observation_ind = i * workgroup_size + local_id;
                    data->local_reduction_lls[local_id] += ''' + ll_function.get_cl_function_name() + '''(
                        data, x, observation_ind);
                }

                barrier(CLK_LOCAL_MEM_FENCE);

                double ll = 0;
                for(uint i = 0; i < workgroup_size; i++){
                    ll += data->local_reduction_lls[i];
                }
                return ll;
            }

            /**
             * Prepare the parameters for evaluation.
             * 
             * This is where the user can enter possible parameter transformations like a modulus transformation.
             *
             * For boundary condition checks please use the ``_step_within_bounds`` function instead.
             *  
             * Args:
             *  data: the data container
             *  params: the parameters we wish to input into the model, changes can be done in place.
             */
            void _prepare_parameters(mot_data_struct* data, mot_float_type* params){
        '''
        if codec is not None:
            func += '''
                encode(data, params);
                decode(data, params);
            '''
        func += '''
            }
            
            /**
             * Evaluate the model with a little perturbation on two axis.
             * 
             * Args:
             *  data: the data container
             *  x_input: the array with the input parameters
             *  perturb_loc_0: the index (into the x_input parameters) of the first parameter to perturbate
             *  perturb_0: the added perturbation of the first parameter, corresponds to ``perturb_loc_0``
             *  perturb_loc_1: the index (into the x_input parameters) of the second parameter to perturbate
             *  perturb_1: the added perturbation of the second parameter, corresponds to ``perturb_loc_1``
             *
             * Returns:
             *  the function evaluated at the parameters plus their perturbation.
             */ 
            double _eval_step(mot_data_struct* data, mot_float_type* x_input, 
                             uint perturb_loc_0, mot_float_type perturb_0,
                             uint perturb_loc_1, mot_float_type perturb_1){
            
                mot_float_type x_tmp[''' + str(nmr_params) + '''];
                for(uint i = 0; i < ''' + str(nmr_params) + '''; i++){
                    x_tmp[i] = x_input[i];
                }
                x_tmp[perturb_loc_0] += perturb_0;
                x_tmp[perturb_loc_1] += perturb_1;
                
                _prepare_parameters(data, x_tmp);
                return _calculate_function(data, x_tmp);                
            }
            
            /** 
             * Compute the Hessian for one row of step sizes.
             *
             * This method uses a central difference method at the 2nd order Taylor expansion. 
             */
            void _compute_step(mot_data_struct* data, mot_float_type* x_input, mot_float_type f_x_input, 
                              global mot_float_type* step_sizes, global double* step_evaluate_ptr){
                
                mot_float_type perturbation[''' + str(nmr_params) + '''];
                double calc;
                uint result_ind = 0;
                
                for(uint px = 0; px < ''' + str(nmr_params) + '''; px++){
                    calc = (
                          _eval_step(data, x_input, 
                                    px, 2 * (step_sizes[px] / data->parameter_scalings[px]),
                                    0, 0)
                        + _eval_step(data, x_input, 
                                    px, -2 * (step_sizes[px] / data->parameter_scalings[px]),
                                    0, 0)
                        - 2 * f_x_input
                        ) / (4 * step_sizes[px] * step_sizes[px]);
                    
                    if(get_local_id(0) == 0){
                        step_evaluate_ptr[result_ind++] = calc;
                    }
                    
                    for(uint py = px + 1; py < ''' + str(nmr_params) + '''; py++){
                        calc = (
                              _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                            - _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                            - _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                            + _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                        ) / (4 * step_sizes[px] * step_sizes[py]);
                        
                        /*
                        calc = (
                            -63 * (
                                _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, -2 * step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, 2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, -2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, 2 * step_sizes[py] / data->parameter_scalings[py])
                            ) +  
                            63 * (
                                _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, -2 * step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, -2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, 2 * step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, 2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                            )  +  
                            44 * (
                                _eval_step(data, x_input, 
                                        px, 2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, -2 * step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, -2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, 2 * step_sizes[py] / data->parameter_scalings[py])
                                - _eval_step(data, x_input, 
                                        px, -2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, -2 * step_sizes[py] / data->parameter_scalings[py])
                                - _eval_step(data, x_input, 
                                        px, 2 * step_sizes[px] / data->parameter_scalings[px],
                                        py, 2 * step_sizes[py] / data->parameter_scalings[py])
                            )  +  
                            74 * (
                                _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                                + _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                                - _eval_step(data, x_input, 
                                        px, step_sizes[px] / data->parameter_scalings[px],
                                        py, -step_sizes[py] / data->parameter_scalings[py])
                                - _eval_step(data, x_input, 
                                        px, -step_sizes[px] / data->parameter_scalings[px],
                                        py, step_sizes[py] / data->parameter_scalings[py])
                            ) 
                        ) / (600 * step_sizes[px] * step_sizes[py]);
                        */
                        
                        if(get_local_id(0) == 0){
                            step_evaluate_ptr[result_ind++] = calc;
                        }
                    }
                }    
            }
            
            /**
             * Apply a simple kernel convolution over the results from each row of steps.
             * 
             * This applies a convolution starting from the starting step index that contained a valid move. 
             * It uses the mode 'reflect' to deal with outside points.
             * 
             * Please note that this kernel is hard coded to work with 2nd order Taylor expansions derivatives only.
             
             * Args:
             *  step_evaluates: the step evaluates for each row of the step sizes
             *  step_evaluates_convoluted: the array to place the convoluted results in 
             */ 
            void _apply_richardson_convolution(
                    global double* step_evaluates,
                    global double* step_evaluates_convoluted){
                    
                double kernel_2[2] = {''' + ', '.join(map(str, richardson_rules[0])) + '''};
                double kernel_3[3] = {''' + ', '.join(map(str, richardson_rules[1])) + '''};
                
                double* kernel_ptr;
                uint kernel_length;
                
                if(''' + str(nmr_steps) + ''' <= 1){
                    return;
                }
                
                if(''' + str(nmr_steps) + ''' == 2){
                    kernel_ptr = kernel_2;
                    kernel_length = 2;
                }
                else{
                    kernel_ptr = kernel_3;
                    kernel_length = 3;
                }
                
                uint kernel_step_ind;
                ulong local_id = get_local_id(0);
                uint workgroup_size = get_local_size(0);
                uint element_ind;
                uint elements_for_workitem = ceil(''' + str(elements_needed) + ''' / (mot_float_type)workgroup_size);
                
                if(workgroup_size * (elements_for_workitem - 1) + local_id >= ''' + str(elements_needed) + '''){
                    elements_for_workitem -= 1;
                }
                
                for(uint i = 0; i < elements_for_workitem; i++){
                    element_ind = i * workgroup_size + local_id;        
                    
                    for(uint step_ind = 0; step_ind < ''' + str(nmr_steps) + '''; step_ind++){
                        
                        // convolute kernel
                        for(uint kernel_ind = 0; kernel_ind < kernel_length; kernel_ind++){
                            kernel_step_ind = step_ind + kernel_ind;
                            
                            // reflect
                            if(kernel_step_ind >= ''' + str(nmr_steps) + '''){
                                kernel_step_ind -= 2 * (kernel_step_ind - ''' + str(nmr_steps) + ''') + 1;  
                            }
                         
                            step_evaluates_convoluted[step_ind * ''' + str(elements_needed) + ''' + element_ind] += 
                                (step_evaluates[kernel_step_ind * ''' + str(elements_needed) + ''' + element_ind] 
                                    * kernel_ptr[kernel_ind]);
                        }
                    }
                }
            }
            
            /**
             * Check if the initial point is within bounds. If not, we can not continue.
             */
            bool _initial_point_within_bounds(mot_data_struct* data, mot_float_type* x){
                for(uint param_ind = 0; param_ind < ''' + str(nmr_steps) + '''; param_ind++){ 
                    if(!''' + bound_check_function.get_cl_function_name() + '''(data, x[param_ind], 0, param_ind)){
                        return false;
                    }
                }
                return true;
            }
            
            /**
             * Generate set of step sizes for each parameter.
             * 
             * This first tries to find for every parameter an initial step size that is within bounds. 
             * Next, it generates steps of decreasing magnitude according to the user specified step ratio until
             * we reached the number of desired steps.
             *
             * This will write the step sizes directly into the step_sizes matrix.
             *
             * Returns:
             *   false if no suitable steps could be generated for this parameter, true if it could.
             */
            bool _generate_grid_points(mot_data_struct* data, mot_float_type* x_input, 
                                       global mot_float_type* step_sizes){
                
                mot_float_type max_steps[''' + str(nmr_params) + '''] = {''' + ', '.join(map(str, max_steps)) + '''};
                mot_float_type first_step;
                bool first_step_found = true;
                
                for(uint param_ind = 0; param_ind < ''' + str(nmr_params) + '''; param_ind++){ 
                    first_step = max_steps[param_ind];
                    
                    // heuristic, try halving the parameter 10 times to see if we can get an acceptable initial step.
                    for(uint i = 0; i < 10; i++){
                        if(''' + bound_check_function.get_cl_function_name() + '''(
                               data, x_input[param_ind], first_step / data->parameter_scalings[param_ind], param_ind)){
                            step_sizes[param_ind] = first_step;
                            first_step_found = true;
                        }
                        else{
                            first_step /= 2.0;
                        }    
                    }
                    if(!first_step_found){
                        return false;
                    }
                    
                    for(uint step_ind = 1; step_ind < ''' + str(nmr_steps) + '''; step_ind++){
                        step_sizes[param_ind + step_ind * ''' + str(nmr_params) + '''] = 
                            first_step / pown(''' + str(float(step_ratio)) + ''', step_ind);
                    }
                }
                return true;
            }
            
            void compute(mot_data_struct* data){
                uint param_ind;
                double eval;
                double f_x_input;

                mot_float_type x_input[''' + str(nmr_params) + '''];
                mot_float_type bound_check_steps[''' + str(nmr_params) + '''];
                local bool within_bounds;
                
                global double* step_evaluate_ptr;
                global double* step_evaluate_convoluted_ptr;
                global mot_float_type* parameter_steps_ptr;
                
                for(param_ind = 0; param_ind < ''' + str(nmr_params) + '''; param_ind++){
                    x_input[param_ind] = data->parameters[param_ind];
                }
                f_x_input = _calculate_function(data, x_input);
                
                if(get_local_id(0) == 0){
                    within_bounds = _initial_point_within_bounds(data, x_input);
                }
                barrier(CLK_GLOBAL_MEM_FENCE);
                if(!within_bounds){
                    return;
                }
                
                if(get_local_id(0) == 0){
                    within_bounds = _generate_grid_points(data, x_input, data->steps);
                }
                barrier(CLK_GLOBAL_MEM_FENCE);
                if(!within_bounds){
                    return;
                }
                                
                for(uint step_ind = 0; step_ind < ''' + str(nmr_steps) + '''; step_ind++){ 
                    parameter_steps_ptr = data->steps + step_ind * ''' + str(nmr_params) + ''';
                    step_evaluate_ptr = data->step_evaluates + step_ind * ''' + str(elements_needed) + ''';
                    step_evaluate_convoluted_ptr = data->step_evaluates_convoluted + step_ind * ''' + str(elements_needed) + ''';
                       
                    _compute_step(data, x_input, f_x_input, parameter_steps_ptr, step_evaluate_ptr);
                }
                barrier(CLK_GLOBAL_MEM_FENCE);
                
                _apply_richardson_convolution(data->step_evaluates, data->step_evaluates_convoluted);
            }
        '''
        return SimpleNamedCLFunction(func, 'compute')


"""
Everything under this comment is copied from numdifftools (https://github.com/pbrod/numdifftools) to remove a dependency.

In the future these extrapolation methods should be translated to OpenCL for fast evaluation over multiple instances.
"""


class Extrapolate(object):

    def __init__(self, step_ratio):
        self._step_ratio = step_ratio

    def __call__(self, el):
        step_evaluates, step_evaluates_convoluted, steps, nmr_params = el
        richardson = Richardson(step_ratio=self._step_ratio, step=2, order=2, num_terms=2)

        if len(step_evaluates) == 0:
            return np.zeros((nmr_params, nmr_params))

        def _vstack(sequence, steps):
            original_shape = np.shape(sequence[0])
            f_del = np.vstack(list(np.ravel(r)) for r in sequence)
            h = np.vstack(list(np.ravel(np.ones(original_shape) * step))
                          for step in steps)
            return f_del, h, original_shape

        def _wynn_extrapolate(der, steps):
            der, errors = dea3(der[0:-2], der[1:-1], der[2:], symmetric=False)
            return der, errors, steps[2:]

        def _add_error_to_outliers(der, trim_fact=10):
            # discard any estimate that differs wildly from the
            # median of all estimates. A factor of 10 to 1 in either
            # direction is probably wild enough here. The actual
            # trimming factor is defined as a parameter.
            try:
                median = np.nanmedian(der, axis=0)
                p75 = np.nanpercentile(der, 75, axis=0)
                p25 = np.nanpercentile(der, 25, axis=0)
                iqr = np.abs(p75 - p25)
            except ValueError as msg:
                return 0 * der

            a_median = np.abs(median)
            outliers = (((abs(der) < (a_median / trim_fact)) +
                         (abs(der) > (a_median * trim_fact))) * (a_median > 1e-8) +
                        ((der < p25 - 1.5 * iqr) + (p75 + 1.5 * iqr < der)))
            errors = outliers * np.abs(der - median)
            return errors

        def _get_arg_min(errors):
            shape = errors.shape
            try:
                arg_mins = np.nanargmin(errors, axis=0)
                min_errors = np.nanmin(errors, axis=0)
            except ValueError as msg:
                return np.arange(shape[1])

            for i, min_error in enumerate(min_errors):
                idx = np.flatnonzero(errors[:, i] == min_error)
                arg_mins[i] = idx[idx.size // 2]
            return np.ravel_multi_index((arg_mins, np.arange(shape[1])), shape)

        def _get_best_estimate(der, errors, steps, shape):
            errors += _add_error_to_outliers(der)
            ix = _get_arg_min(errors)
            final_step = steps.flat[ix].reshape(shape)
            err = errors.flat[ix].reshape(shape)
            return der.flat[ix].reshape(shape), final_step, err

        r_conv, _, _ = _vstack(step_evaluates_convoluted, steps)
        results, steps, shape = _vstack(step_evaluates, steps)

        der1, errors1, steps = richardson(results, r_conv, steps)
        if len(der1) > 2:
            der1, errors1, steps = _wynn_extrapolate(der1, steps)
        der, final_step, err = _get_best_estimate(der1, errors1, steps, shape)
        return der


EPS = np.finfo(float).eps
_EPS = EPS
_TINY = np.finfo(float).tiny


def max_abs(a1, a2):
    return np.maximum(np.abs(a1), np.abs(a2))


def dea3(v0, v1, v2, symmetric=False):
    """
    Extrapolate a slowly convergent sequence

    Parameters
    ----------
    v0, v1, v2 : array-like
        3 values of a convergent sequence to extrapolate

    Returns
    -------
    result : array-like
        extrapolated value
    abserr : array-like
        absolute error estimate

    Description
    -----------
    DEA3 attempts to extrapolate nonlinearly to a better estimate
    of the sequence's limiting value, thus improving the rate of
    convergence. The routine is based on the epsilon algorithm of
    P. Wynn, see [1]_.

     Example
     -------
     # integrate sin(x) from 0 to pi/2

     >>> import numpy as np
     >>> import numdifftools as nd
     >>> Ei= np.zeros(3)
     >>> linfun = lambda i : np.linspace(0, np.pi/2., 2**(i+5)+1)
     >>> for k in np.arange(3):
     ...    x = linfun(k)
     ...    Ei[k] = np.trapz(np.sin(x),x)
     >>> [En, err] = nd.dea3(Ei[0], Ei[1], Ei[2])
     >>> truErr = Ei-1.
     >>> (truErr, err, En)
     (array([ -2.00805680e-04,  -5.01999079e-05,  -1.25498825e-05]),
     array([ 0.00020081]), array([ 1.]))

     See also
     --------
     dea

     Reference
     ---------
     .. [1] C. Brezinski and M. Redivo Zaglia (1991)
            "Extrapolation Methods. Theory and Practice", North-Holland.

    ..  [2] C. Brezinski (1977)
            "Acceleration de la convergence en analyse numerique",
            "Lecture Notes in Math.", vol. 584,
            Springer-Verlag, New York, 1977.

    ..  [3] E. J. Weniger (1989)
            "Nonlinear sequence transformations for the acceleration of
            convergence and the summation of divergent series"
            Computer Physics Reports Vol. 10, 189 - 371
            http://arxiv.org/abs/math/0306302v1
    """
    e0, e1, e2 = np.atleast_1d(v0, v1, v2)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ignore division by zero and overflow
        delta2, delta1 = e2 - e1, e1 - e0
        err2, err1 = np.abs(delta2), np.abs(delta1)
        tol2, tol1 = max_abs(e2, e1) * _EPS, max_abs(e1, e0) * _EPS
        delta1[err1 < _TINY] = _TINY
        delta2[err2 < _TINY] = _TINY  # avoid division by zero and overflow
        ss = 1.0 / delta2 - 1.0 / delta1 + _TINY
        smalle2 = abs(ss * e1) <= 1.0e-3
        converged = (err1 <= tol1) & (err2 <= tol2) | smalle2
        result = np.where(converged, e2 * 1.0, e1 + 1.0 / ss)
    abserr = err1 + err2 + np.where(converged, tol2 * 10, np.abs(result - e2))
    if symmetric and len(result) > 1:
        return result[:-1], abserr[1:]
    return result, abserr


class Richardson(object):

    """
    Extrapolates as sequence with Richardsons method

    Notes
    -----
    Suppose you have series expansion that goes like this

    L = f(h) + a0 * h^p_0 + a1 * h^p_1+ a2 * h^p_2 + ...

    where p_i = order + step * i  and f(h) -> L as h -> 0, but f(0) != L.

    If we evaluate the right hand side for different stepsizes h
    we can fit a polynomial to that sequence of approximations.
    This is exactly what this class does.

    Example
    -------
    >>> import numpy as np
    >>> import numdifftools as nd
    >>> n = 3
    >>> Ei = np.zeros((n,1))
    >>> h = np.zeros((n,1))
    >>> linfun = lambda i : np.linspace(0, np.pi/2., 2**(i+5)+1)
    >>> for k in np.arange(n):
    ...    x = linfun(k)
    ...    h[k] = x[1]
    ...    Ei[k] = np.trapz(np.sin(x),x)
    >>> En, err, step = nd.Richardson(step=1, order=1)(Ei, h)
    >>> truErr = Ei-1.
    >>> (truErr, err, En)
    (array([[ -2.00805680e-04],
           [ -5.01999079e-05],
           [ -1.25498825e-05]]), array([[ 0.00160242]]), array([[ 1.]]))

    """

    def __init__(self, step_ratio=2.0, step=1, order=1, num_terms=2):
        self.num_terms = num_terms
        self.order = order
        self.step = step
        self.step_ratio = step_ratio

    def _r_matrix(self, num_terms):
        step = self.step
        i, j = np.ogrid[0:num_terms + 1, 0:num_terms]
        r_mat = np.ones((num_terms + 1, num_terms + 1))
        r_mat[:, 1:] = (1.0 / self.step_ratio) ** (i * (step * j + self.order))
        return r_mat

    def rule(self, sequence_length=None):
        if sequence_length is None:
            sequence_length = self.num_terms + 1
        num_terms = min(self.num_terms, sequence_length - 1)
        if num_terms > 0:
            r_mat = self._r_matrix(num_terms)
            return linalg.pinv(r_mat)[0]
        return np.ones((1,))

    @staticmethod
    def _estimate_error(new_sequence, old_sequence, steps, rule):
        m = new_sequence.shape[0]
        mo = old_sequence.shape[0]
        cov1 = np.sum(rule**2)  # 1 spare dof
        fact = np.maximum(12.7062047361747 * np.sqrt(cov1), EPS * 10.)
        if mo < 2:
            return (np.abs(new_sequence) * EPS + steps) * fact
        if m < 2:
            delta = np.diff(old_sequence, axis=0)
            tol = max_abs(old_sequence[:-1], old_sequence[1:]) * fact
            err = np.abs(delta)
            converged = err <= tol
            abserr = err[-m:] + np.where(converged[-m:], tol[-m:] * 10,
                                abs(new_sequence - old_sequence[-m:]) * fact)
            return abserr
#         if mo>2:
#             res, abserr = dea3(old_sequence[:-2], old_sequence[1:-1],
#                               old_sequence[2:] )
#             return abserr[-m:] * fact
        err = np.abs(np.diff(new_sequence, axis=0)) * fact
        tol = max_abs(new_sequence[1:], new_sequence[:-1]) * EPS * fact
        converged = err <= tol
        abserr = err + np.where(converged, tol * 10,
                                abs(new_sequence[:-1] -
                                    old_sequence[-m+1:]) * fact)
        return abserr

    def extrapolate(self, sequence, steps):
        return self.__call__(sequence, steps)

    def __call__(self, sequence, new_sequence, steps):
        ne = sequence.shape[0]
        rule = self.rule(ne)
        nr = rule.size - 1
        m = ne - nr
        mm = min(ne, m+1)
        abserr = self._estimate_error(new_sequence[:mm], sequence, steps, rule)
        return new_sequence[:m], abserr[:m], steps[:m]