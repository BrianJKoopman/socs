import argparse
import time
from dataclasses import dataclass
from queue import Queue

import txaio
from twisted.internet import defer

txaio.use_twisted()

from ocs import ocs_agent, site_config

import socs.agents.hwp_pcu.drivers.hwp_pcu as pcu


class Actions:
    class BaseAction:
        def __post_init__(self):
            self.deferred = defer.Deferred()
            self.log = txaio.make_logger()

    @dataclass
    class SendCommand (BaseAction):
        command: str


def process_action(action, PCU: pcu.PCU):
    """Process an action with PCU hardware"""
    if isinstance(action, Actions.SendCommand):
        off_channel = []
        on_channel = []
        if action.command == 'off':
            off_channel = [0, 1, 2, 5, 6, 7]
            on_channel = []
        elif action.command == 'on_1':
            off_channel = [5, 6, 7]
            on_channel = [0, 1, 2]
        elif action.command == 'on_2':
            on_channel = [0, 1, 2, 5, 6, 7]
            off_channel = []
        elif action.command == 'stop':
            on_channel = [1, 2, 5]
            off_channel = [0, 6, 7]

        action.log.info(f"Command: {action.command}")
        action.log.info(f"  Off channels: {off_channel}")
        action.log.info(f"  On channels: {on_channel}")
        for i in off_channel:
            PCU.relay_off(i)
        for i in on_channel:
            PCU.relay_on(i)

        return dict(off_channel=off_channel, on_channel=on_channel)


class HWPPCUAgent:
    """Agent to phase compensation improve the CHWP motor efficiency

    Args:
        agent (ocs.ocs_agent.OCSAgent): Instantiated OCSAgent class for this agent
        port (str): Path to USB device in '/dev/'
    """

    def __init__(self, agent, port):
        self.agent: ocs_agent.OCSAgent = agent
        self.log = agent.log
        self.port = port
        self.action_queue = Queue()

        agg_params = {'frame_length': 60}
        self.agent.register_feed(
            'hwppcu', record=True, agg_params=agg_params)

    @defer.inlineCallbacks
    @ocs_agent.param('command', default='off', type=str, choices=['off', 'on_1', 'on_2', 'stop'])
    def send_command(self, session, params):
        """send_command(command)

        **Task** - Send commands to the phase compensation unit.
        off: The compensation phase is zero.
        on_1: The compensation phase is +120 deg.
        on_2: The compensation phase is -120 deg.
        stop: Stop the HWP spin.

        Parameters:
            command (str): set the operation mode from 'off', 'on_1', 'on_2' or 'stop'.

        """
        action = Actions.SendCommand(**params)
        self.action_queue.put(action)
        session.data = yield action.deferred
        return True, f"Set relays for cmd={action.command}"

    def _process_actions(self, PCU: pcu.PCU):
        while not self.action_queue.empty():
            action = self.action_queue.get()
            try:
                self.log.info(f"Running action {action}")
                res = process_action(action, PCU)
                action.deferred.callback(res)
            except Exception as e:
                self.log.error(f"Error processing action: {action}")
                action.deferred.errback(e)

    def _get_and_publish_data(self, PCU: pcu.PCU, session):
        now = time.time()
        data = {'timestamp': now,
                'block_name': 'hwppcu',
                'data': {}}
        status = PCU.get_status()
        data['data']['status'] = status
        self.agent.publish_to_feed('hwppcu', data)
        session.data = {'status': status, 'last_updated': now}

    def main(self, session, params):
        """
        **Process** - Main process for PCU agent.
        """
        PCU = pcu.PCU(port=self.port)
        self.log.info('Connected to PCU')

        session.set_status('running')
        while not self.action_queue.empty():
            action = self.action_queue.get()
            action.deferred.errback(Exception("Action cancelled"))

        last_daq = 0
        while session.status in ['starting', 'running']:
            now = time.time()
            if now - last_daq > 5:
                self._get_and_publish_data(PCU, session)
                last_daq = now

            self._process_actions(PCU)
            time.sleep(0.1)

        PCU.close()

    def _stop_main(self, session, params):
        """
        Stop acq process.
        """
        session.set_status('stopping')
        return True, 'Set main status to stopping'


def make_parser(parser=None):
    """
    Build the argument parser for the Agent. Allows sphinx to automatically build documentation
    baised on this function
    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port', type=str, help="Path to USB node for the PCU")
    return parser


def main(args=None):
    parser = make_parser()
    args = site_config.parse_args(agent_class='HWPPCUAgent',
                                  parser=parser,
                                  args=args)

    agent, runner = ocs_agent.init_site_agent(args)
    hwppcu_agent = HWPPCUAgent(agent,
                               port=args.port)
    agent.register_task('send_command', hwppcu_agent.send_command, blocking=False)
    agent.register_process(
        'main', hwppcu_agent.main, hwppcu_agent._stop_main, startup=True)

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()