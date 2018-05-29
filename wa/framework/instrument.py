#    Copyright 2013-2015 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""
Adding New Instrument
=====================

Any new instrument should be a subclass of Instrument and it must have a name.
When a new instrument is added to Workload Automation, the methods of the new
instrument will be found automatically and hooked up to the supported signals.
Once a signal is broadcasted, the corresponding registered method is invoked.

Each method in Instrument must take two arguments, which are self and context.
Supported signals can be found in [... link to signals ...] To make
implementations easier and common, the basic steps to add new instrument is
similar to the steps to add new workload.

Hence, the following methods are sufficient to implement to add new instrument:

    - setup: This method is invoked after the workload is setup. All the
       necessary setups should go inside this method. Setup, includes operations
       like, pushing the files to the target device, install them, clear logs,
       etc.
    - start: It is invoked just before the workload start execution. Here is
       where instrument measures start being registered/taken.
    - stop: It is invoked just after the workload execution stops. The measures
       should stop being taken/registered.
    - update_output: It is invoked after the workload updated its result.
       update_output is where the taken measures are added to the output so it
       can be processed by Workload Automation.
    - teardown is invoked after the workload is teared down. It is a good place
       to clean any logs generated by the instrument.

For example, to add an instrument which will trace device errors, we subclass
Instrument and overwrite the variable name.::

        #BINARY_FILE = os.path.join(os.path.dirname(__file__), 'trace')
        class TraceErrorsInstrument(Instrument):

            name = 'trace-errors'

            def __init__(self, device):
                super(TraceErrorsInstrument, self).__init__(device)
                self.trace_on_device = os.path.join(self.device.working_directory, 'trace')

We then declare and implement the aforementioned methods. For the setup method,
we want to push the file to the target device and then change the file mode to
755 ::

    def setup(self, context):
        self.device.push(BINARY_FILE, self.device.working_directory)
        self.device.execute('chmod 755 {}'.format(self.trace_on_device))

Then we implemented the start method, which will simply run the file to start
tracing. ::

    def start(self, context):
        self.device.execute('{} start'.format(self.trace_on_device))

Lastly, we need to stop tracing once the workload stops and this happens in the
stop method::

    def stop(self, context):
        self.device.execute('{} stop'.format(self.trace_on_device))

The generated output can be updated inside update_output, or if it is trace, we
just pull the file to the host device. context has an output variable which
has add_metric method. It can be used to add the instruments results metrics
to the final result for the workload. The method can be passed 4 params, which
are metric key, value, unit and lower_is_better, which is a boolean. ::

    def update_output(self, context):
        # pull the trace file to the device
        result = os.path.join(self.device.working_directory, 'trace.txt')
        self.device.pull(result, context.working_directory)

        # parse the file if needs to be parsed, or add result to
        # context.result

At the end, we might want to delete any files generated by the instruments
and the code to clear these file goes in teardown method. ::

    def teardown(self, context):
        self.device.remove(os.path.join(self.device.working_directory, 'trace.txt'))

"""

import logging
import inspect
from collections import OrderedDict

from wa.framework import signal
from wa.framework.plugin import Plugin
from wa.framework.exception import (WAError, TargetNotRespondingError, TimeoutError,
                                    WorkloadError, TargetError)
from wa.utils.log import log_error
from wa.utils.misc import isiterable
from wa.utils.types import identifier, enum, level


logger = logging.getLogger('instruments')


# Maps method names onto signals the should be registered to.
# Note: the begin/end signals are paired -- if a begin_ signal is sent,
#       then the corresponding end_ signal is guaranteed to also be sent.
# Note: using OrderedDict to preserve logical ordering for the table generated
#       in the documentation
SIGNAL_MAP = OrderedDict([
    # Below are "aliases" for some of the more common signals to allow
    # instruments to have similar structure to workloads
    ('initialize', signal.RUN_INITIALIZED),
    ('setup', signal.BEFORE_WORKLOAD_SETUP),
    ('start', signal.BEFORE_WORKLOAD_EXECUTION),
    ('stop', signal.AFTER_WORKLOAD_EXECUTION),
    ('process_workload_output', signal.SUCCESSFUL_WORKLOAD_OUTPUT_UPDATE),
    ('update_output', signal.AFTER_WORKLOAD_OUTPUT_UPDATE),
    ('teardown', signal.AFTER_WORKLOAD_TEARDOWN),
    ('finalize', signal.RUN_FINALIZED),

    ('on_run_start', signal.RUN_STARTED),
    ('on_run_end', signal.RUN_COMPLETED),

    ('on_job_start', signal.JOB_STARTED),
    ('on_job_restart', signal.JOB_RESTARTED),
    ('on_job_end', signal.JOB_COMPLETED),
    ('on_job_falure', signal.JOB_FAILED),
    ('on_job_abort', signal.JOB_ABORTED),

    ('before_job', signal.BEFORE_JOB),
    ('on_successful_job', signal.SUCCESSFUL_JOB),
    ('after_job', signal.AFTER_JOB),
    ('before_processing_job_output', signal.BEFORE_JOB_OUTPUT_PROCESSED),
    ('on_successfully_processing_job', signal.SUCCESSFUL_JOB_OUTPUT_PROCESSED),
    ('after_processing_job_output', signal.AFTER_JOB_OUTPUT_PROCESSED),

    ('before_reboot', signal.BEFORE_REBOOT),
    ('on_successful_reboot', signal.SUCCESSFUL_REBOOT),
    ('after_reboot', signal.AFTER_REBOOT),

    ('on_error', signal.ERROR_LOGGED),
    ('on_warning', signal.WARNING_LOGGED),
])


Priority = enum(['very_slow', 'slow', 'normal', 'fast', 'very_fast'], -20, 10)


def get_priority(func):
    return getattr(getattr(func, 'im_func', func),
                   'priority', Priority.normal)


def priority(priority):
    def decorate(func):
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        wrapper.func_name = func.func_name
        if priority in Priority.levels:
            wrapper.priority = Priority(priority)
        else:
            if not isinstance(priority, int):
                msg = 'Invalid priorty "{}"; must be an int or one of {}'
                raise ValueError(msg.format(priority, Priority.values))
            wrapper.priority = level('custom', priority)
        return wrapper
    return decorate


very_slow = priority(Priority.very_slow)
slow = priority(Priority.slow)
normal = priority(Priority.normal)
fast = priority(Priority.fast)
very_fast = priority(Priority.very_fast)


installed = []


def is_installed(instrument):
    if isinstance(instrument, Instrument):
        if instrument in installed:
            return True
        if instrument.name in [i.name for i in installed]:
            return True
    elif isinstance(instrument, type):
        if instrument in [i.__class__ for i in installed]:
            return True
    else:  # assume string
        if identifier(instrument) in [identifier(i.name) for i in installed]:
            return True
    return False


def is_enabled(instrument):
    if isinstance(instrument, Instrument) or isinstance(instrument, type):
        name = instrument.name
    else:  # assume string
        name = instrument
    try:
        installed_instrument = get_instrument(name)
        return installed_instrument.is_enabled
    except ValueError:
        return False


failures_detected = False


def reset_failures():
    global failures_detected  # pylint: disable=W0603
    failures_detected = False


def check_failures():
    result = failures_detected
    reset_failures()
    return result


class ManagedCallback(object):
    """
    This wraps instruments' callbacks to ensure that errors do not interfer
    with run execution.

    """

    def __init__(self, instrument, callback):
        self.instrument = instrument
        self.callback = callback

    def __call__(self, context):
        if self.instrument.is_enabled:
            try:
                if not context.tm.is_responsive:
                    logger.debug("Target unreponsive; skipping callback {}".format(self.callback))
                    return
                self.callback(context)
            except (KeyboardInterrupt, TargetNotRespondingError, TimeoutError):  # pylint: disable=W0703
                raise
            except Exception as e:  # pylint: disable=W0703
                logger.error('Error in instrument {}'.format(self.instrument.name))
                global failures_detected  # pylint: disable=W0603
                failures_detected = True
                log_error(e, logger)
                context.add_event(e.message)
                if isinstance(e, WorkloadError):
                    context.set_status('FAILED')
                elif isinstance(e, TargetError) or isinstance(e, TimeoutError):
                    context.tm.verify_target_responsive(context.reboot_policy.can_reboot)
                else:
                    if context.current_job:
                        context.set_status('PARTIAL')
                    else:
                        raise

    def __repr__(self):
        text = 'ManagedCallback({}, {})'
        return text.format(self.instrument.name, self.callback.im_func.func_name)

    __str__ = __repr__


# Need this to keep track of callbacks, because the dispatcher only keeps
# weak references, so if the callbacks aren't referenced elsewhere, they will
# be deallocated before they've had a chance to be invoked.
_callbacks = []


def install(instrument, context):
    """
    This will look for methods (or any callable members) with specific names
    in the instrument and hook them up to the corresponding signals.

    :param instrument: Instrument instance to install.

    """
    logger.debug('Installing instrument %s.', instrument)

    if is_installed(instrument):
        msg = 'Instrument {} is already installed.'
        raise ValueError(msg.format(instrument.name))

    for attr_name in dir(instrument):
        if attr_name not in SIGNAL_MAP:
            continue

        attr = getattr(instrument, attr_name)

        if not callable(attr):
            msg = 'Attribute {} not callable in {}.'
            raise ValueError(msg.format(attr_name, instrument))
        argspec = inspect.getargspec(attr)
        arg_num = len(argspec.args)
        # Instrument callbacks will be passed exactly two arguments: self
        # (the instrument instance to which the callback is bound) and
        # context. However, we also allow callbacks to capture the context
        # in variable arguments (declared as "*args" in the definition).
        if arg_num > 2 or (arg_num < 2 and argspec.varargs is None):
            message = '{} must take exactly 2 positional arguments; {} given.'
            raise ValueError(message.format(attr_name, arg_num))

        priority = get_priority(attr)
        logger.debug('\tConnecting %s to %s with priority %s(%d)', attr.__name__,
                     SIGNAL_MAP[attr_name], priority.name, priority.value)

        mc = ManagedCallback(instrument, attr)
        _callbacks.append(mc)
        signal.connect(mc, SIGNAL_MAP[attr_name], priority=priority.value)

    instrument.logger.context = context
    installed.append(instrument)


def uninstall(instrument):
    instrument = get_instrument(instrument)
    installed.remove(instrument)


def validate():
    for instrument in installed:
        instrument.validate()


def get_instrument(inst):
    if isinstance(inst, Instrument):
        return inst
    for installed_inst in installed:
        if identifier(installed_inst.name) == identifier(inst):
            return installed_inst
    raise ValueError('Instrument {} is not installed'.format(inst))


def disable_all():
    for instrument in installed:
        _disable_instrument(instrument)


def enable_all():
    for instrument in installed:
        _enable_instrument(instrument)


def enable(to_enable):
    if isiterable(to_enable):
        for inst in to_enable:
            _enable_instrument(inst)
    else:
        _enable_instrument(to_enable)


def disable(to_disable):
    if isiterable(to_disable):
        for inst in to_disable:
            _disable_instrument(inst)
    else:
        _disable_instrument(to_disable)


def _enable_instrument(inst):
    inst = get_instrument(inst)
    if not inst.is_broken:
        logger.debug('Enabling instrument {}'.format(inst.name))
        inst.is_enabled = True
    else:
        logger.debug('Not enabling broken instrument {}'.format(inst.name))


def _disable_instrument(inst):
    inst = get_instrument(inst)
    if inst.is_enabled:
        logger.debug('Disabling instrument {}'.format(inst.name))
        inst.is_enabled = False


def get_enabled():
    return [i for i in installed if i.is_enabled]


def get_disabled():
    return [i for i in installed if not i.is_enabled]


class Instrument(Plugin):
    """
    Base class for instrument implementations.
    """
    kind = "instrument"

    def __init__(self, target, **kwargs):
        super(Instrument, self).__init__(**kwargs)
        self.target = target
        self.is_enabled = True
        self.is_broken = False
