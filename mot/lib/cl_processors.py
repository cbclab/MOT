__author__ = 'Robbert Harms'
__date__ = '2020-01-25'
__maintainer__ = 'Robbert Harms'
__email__ = 'robbert.harms@maastrichtuniversity.nl'
__licence__ = 'LGPL v3'

import pyopencl as cl


class Processor:

    def process(self, is_blocking=False, wait_for=None):
        """Enqueue all compute kernels for this processor.

        This may enqueue multiple kernels to multiple devices.

        Args:
            wait_for (Dict[CLEnvironment: cl.Event]): mapping CLEnvironments to events we should wait
                on before enqueuing work. This will wait on all events sharing the same context.
            is_blocking (boolean): if the call to the processor is a blocking call or not

        Returns:
            Dict[CLEnvironment: cl.Event]: events generated by this processor
        """
        raise NotImplementedError()

    def flush(self):
        """Enqueues a flush operation to all the queues."""
        raise NotImplementedError()

    def finish(self):
        """Enqueues a finish operation to all the queues."""
        raise NotImplementedError()


class ProcessKernel(Processor):

    def __init__(self, kernel, kernel_data, cl_environment, global_nmr_instances, workgroup_size, instance_offset=None):
        """Simple processor which can execute the provided (compiled) kernel with the provided data.

        Args:
            kernel: a pyopencl compiled kernel program
            kernel_data (List[mot.lib.utils.KernelData]): the kernel data to load as input to the kernel
            cl_environment (mot.lib.cl_environments.CLEnvironment): the CL environment to use for executing the kernel
            global_nmr_instances (int): the global work size, this will internally be multiplied by the
                local workgroup size.
            workgroup_size (int): the local size (workgroup size) the kernel must use
            instance_offset (int): the offset for the global id, this will be multiplied with the local workgroup size.
        """
        self._kernel = kernel
        self._kernel_data = kernel_data
        self._cl_environment = cl_environment
        self._global_nmr_instances = global_nmr_instances
        self._instance_offset = instance_offset or 0
        self._kernel.set_scalar_arg_dtypes(self._flatten_list([d.get_scalar_arg_dtypes() for d in self._kernel_data]))
        self._workgroup_size = workgroup_size

    def process(self, is_blocking=False, wait_for=None):
        wait_for = wait_for or {}
        if self._cl_environment in wait_for:
            wait_for = [wait_for[self._cl_environment]]
        else:
            wait_for = None

        event = self._kernel(
            self._cl_environment.queue,
            (int(self._global_nmr_instances * self._workgroup_size),),
            (int(self._workgroup_size),),
            *self._flatten_list([data.get_kernel_inputs(self._cl_environment, self._workgroup_size)
                                 for data in self._kernel_data]),
            global_offset=(int(self._instance_offset * self._workgroup_size),),
            wait_for=wait_for)

        if is_blocking:
            event.wait()

        return {self._cl_environment: event}

    def flush(self):
        self._cl_environment.queue.flush()

    def finish(self):
        self._cl_environment.queue.finish()

    def _flatten_list(self, l):
        return_l = []
        for e in l:
            return_l.extend(e)
        return return_l


class DeviceAccess(Processor):

    def __init__(self, kernel_data, cl_environments):
        """A processor to enqueue device access for all the provided kernel data.

        Args:
            kernel_data (List[mot.lib.utils.KernelData]): the input data for the kernels
            cl_environments (List[mot.lib.cl_environments.CLEnvironment]): the list of CL environment to use
                for executing the kernel
        """
        self._kernel_data = kernel_data
        self._cl_environments = cl_environments

    def process(self, is_blocking=False, wait_for=None):
        events = None
        for ind, kernel_data in enumerate(self._kernel_data):
            events = kernel_data.enqueue_device_access(self._cl_environments, is_blocking=False, wait_for=wait_for)
        return events

    def flush(self):
        for env in self._cl_environments:
            env.queue.flush()

    def finish(self):
        for env in self._cl_environments:
            env.queue.finish()


class HostAccess(Processor):

    def __init__(self, kernel_data, cl_environments):
        """A processor to enqueue device access for all the provided kernel data.

        Args:
            kernel_data (List[mot.lib.utils.KernelData]): the input data for the kernels
            cl_environments (List[mot.lib.cl_environments.CLEnvironment]): the list of CL environment to use
                for executing the kernel
        """
        self._kernel_data = kernel_data
        self._cl_environments = cl_environments

    def process(self, is_blocking=False, wait_for=None):
        events = None
        for ind, kernel_data in enumerate(self._kernel_data):
            events = kernel_data.enqueue_host_access(self._cl_environments, is_blocking=False, wait_for=wait_for)
        return events

    def flush(self):
        for env in self._cl_environments:
            env.queue.flush()

    def finish(self):
        for env in self._cl_environments:
            env.queue.finish()


class MultiDeviceProcessor(Processor):

    def __init__(self, kernels, context_init_kernels,  kernel_data,
                 cl_environments, load_balancer, nmr_instances, use_local_reduction=False, local_size=None,
                 context_variables=None, do_data_transfers=True):
        """Create a processor for the given function and inputs.

        Args:
            kernels (dict): for each CL environment the kernel to use
            kernel_data (dict): the input data for the kernels
            cl_environments (List[mot.lib.cl_environments.CLEnvironment]): the list of CL environment to use
                for executing the kernel
            load_balancer (mot.lib.load_balancers.LoadBalancer): the load balancer to use
            nmr_instances (int): the number of parallel processes to run.
            use_local_reduction (boolean): set this to True if you want to use local memory reduction in
                 evaluating this function. If this is set to True we will multiply the global size
                 (given by the nmr_instances) by the work group sizes.
            local_size (int): can be used to specify the exact local size (workgroup size) the kernel must use.
            do_data_transfers (boolean): if we should do data transfers from host to device and back for evaluating
                this function. For better control set this to False and use the method
                ``enqueue_device_access()`` and ``enqueue_host_access`` of the KernelData to set the data.
        """
        self._subprocessors = []
        self._do_data_transfers = do_data_transfers
        self._kernel_data = kernel_data
        self._cl_environments = cl_environments
        self._context_variables = context_variables

        batches = load_balancer.get_division(cl_environments, nmr_instances)
        for ind, cl_environment in enumerate(cl_environments):
            kernel = kernels[cl_environment]

            if use_local_reduction:
                if local_size:
                    workgroup_size = local_size
                else:
                    workgroup_size = kernel.get_work_group_info(
                        cl.kernel_work_group_info.PREFERRED_WORK_GROUP_SIZE_MULTIPLE, cl_environment.device)
            else:
                workgroup_size = 1

            batch_start, batch_end = batches[ind]
            if batch_end - batch_start > 0:
                if context_variables:
                    context_kernel = context_init_kernels[cl_environment]
                    worker = ProcessKernel(context_kernel, context_variables.values(),
                                           cl_environment, batch_end - batch_start, 1, instance_offset=batch_start)
                    self._subprocessors.append(worker)

                processor = ProcessKernel(kernel, kernel_data.values(), cl_environment,
                                          batch_end - batch_start, workgroup_size, instance_offset=batch_start)
                self._subprocessors.append(processor)

    def process(self, is_blocking=False, wait_for=None):
        if self._do_data_transfers:
            for kernel_data in self._kernel_data.values():
                wait_for = kernel_data.enqueue_device_access(self._cl_environments, is_blocking=False,
                                                             wait_for=wait_for)

            if self._context_variables:
                for kernel_data in self._context_variables.values():
                    wait_for = kernel_data.enqueue_device_access(self._cl_environments, is_blocking=False,
                                                                 wait_for=wait_for)

        events = {}
        for worker in self._subprocessors:
            events.update(worker.process(wait_for=wait_for))
            worker.flush()

        if self._do_data_transfers:
            for ind, kernel_data in enumerate(self._kernel_data.values()):
                events = kernel_data.enqueue_host_access(self._cl_environments, is_blocking=False, wait_for=events)

            if self._context_variables:
                for kernel_data in self._context_variables.values():
                    wait_for = kernel_data.enqueue_host_access(self._cl_environments, is_blocking=False,
                                                               wait_for=wait_for)

        return events

    def flush(self):
        for worker in self._subprocessors:
            worker.flush()

    def finish(self):
        for worker in self._subprocessors:
            worker.finish()
