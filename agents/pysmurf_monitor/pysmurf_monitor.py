from ocs import ocs_agent, site_config
import json
from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor
import datetime

import socs
from socs.db import pysmurf_files_manager

from twisted.python.failure import Failure
import os
import mysql.connector
import argparse

from twisted.enterprise import adbapi

from socs.util import get_md5sum


class PysmurfMonitor(DatagramProtocol):
    """
    Monitor for pysmurf UDP publisher.

    This agent should be run on the smurf-server host, and will monitor messages
    published via the pysmurf Publisher.

    One of its main functions is to register files that pysmurf writes into the
    pysmurf_files database.

    Arguments
    ---------
    agent: ocs.ocs_agent.OCSAgent
        OCSAgent object which is running
    args: Namespace
        argparse namespace with site_config and agent specific arguments\

    Attributes
    -----------
    agent: ocs.ocs_agent.OCSAgent
        OCSAgent object which is running
    log: txaio.tx.Logger
        txaio logger object created by agent
    base_file_info: dict
        shared file info added to all file entries registered by this agent
    dbpool: twisted.enterprise.adbapi.ConnectionPool
        DB connection pool
    """
    def __init__(self, agent, args):
        self.agent: ocs_agent.OCSAgent = agent
        self.log = agent.log

        self.create_table = bool(args.create_table)

        site, instance = self.agent.agent_address.split('.')
        self.base_file_info = {
            'site': site,
            'instance_id': instance,
            'copied': 0,
            'failed_copy_attempts': 0,
            'socs_version': socs.__version__
        }

        sql_config = {
            'user': os.environ["MYSQL_USER"],
            'passwd': os.environ["MYSQL_PASSWORD"],
            'database': 'files'
        }
        db_host = os.environ.get('MYSQL_HOST')
        if db_host is not None:
            sql_config['host'] = db_host

        self.dbpool = adbapi.ConnectionPool('mysql.connector', **sql_config)

    def _add_file_callback(self, res, d):
        """Callback for when a file is successfully added to DB"""
        self.log.info("Added {} to pysmurf_files".format(d['path']))

    def _add_file_errback(self, failure: Failure, d):
        """Errback for when there is an exception when adding file to DB"""
        self.log.error(f"ERROR!!! {d['path']} was not added to the database")
        return failure

    def datagramReceived(self, _data, addr):
        """
        Called whenever UDP data is received.

        Arguments
        ----------
        _data:
            Raw data passed over UDP port. Pysmurf publisher will send a JSON
            string
        addr: tuple
            (host, port) of the sender.
        """
        data = json.loads(_data)

        if data['type'] in ['data_file']:
            self.log.info("New file: {fname}", fname=data['payload']['path'])
            d = data['payload'].copy()

            # Adds additional db info to dict
            d['timestamp'] = datetime.datetime.utcfromtimestamp(d['timestamp'])
            d['md5sum'] = get_md5sum(d['path'])
            d['plot'] = int(d['plot'])
            d.update(self.base_file_info)

            deferred = self.dbpool.runInteraction(pysmurf_files_manager.add_entry, d)
            deferred.addErrback(self._add_file_errback, d)
            deferred.addCallback(self._add_file_callback, d)

    def init(self, session, params=None):
        """
        Initizes agent. If self.create_table, attempts to create pysmurf_files
        if it doesn't already exist. Will update with new cols if any have been
        added to ``socs.db.pysmurf_files_manager``. Will not alter existing cols
        if their name or datatype has been changed.

        Parameters
        ----------
        create_table: bool
            If true will attempt to create/update pysmurf_files table.
        """
        if params is None:
            params = {}

        if params.get('create_table', self.create_table):
            con: mysql.connector.MySQLConnection = self.dbpool.connect()
            cur = con.cursor()

            try:
                pysmurf_files_manager.create_table(cur, update=True)
                con.commit()
            finally:
                self.dbpool.disconnect(con)

        return True, "Initialized agent"


def make_parser(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Config')
    pgroup.add_argument('--udp-port', type=int,
                        help="Port for upd-publisher")
    pgroup.add_argument('--create-table', type=bool,
                        help="Specifies whether agent should create pysmurf_files"
                             "table if none exist.", default=True)

    return parser


if __name__ == '__main__':
    parser = site_config.add_arguments()

    parser = make_parser(parser)

    args = parser.parse_args()

    site_config.reparse_args(args, 'PysmurfMonitor')

    agent, runner = ocs_agent.init_site_agent(args)
    monitor = PysmurfMonitor(agent, args)

    agent.register_task('init', monitor.init, startup=True)

    reactor.listenUDP(args.udp_port, monitor)

    runner.run(agent, auto_reconnect=True)
