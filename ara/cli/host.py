#   Copyright Red Hat, Inc. All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#

import logging
import six

from cliff.lister import Lister
from cliff.show import ShowOne
from ara import app, db, models, utils


class HostList(Lister):
    """Returns a list of hosts"""
    log = logging.getLogger(__name__)

    def get_parser(self, prog_name):
        parser = super(HostList, self).get_parser(prog_name)
        return parser

    def take_action(self, parsed_args):
        hosts = models.Host.query.all()

        fields = (
            ('ID',),
            ('Name',),
        )

        return ([field[0] for field in fields],
                [[utils.get_field_attr(host, field)
                  for field in fields] for host in hosts])


class HostShow(ShowOne):
    """Show details of a host"""
    log = logging.getLogger(__name__)

    def get_parser(self, prog_name):
        parser = super(HostShow, self).get_parser(prog_name)
        parser.add_argument(
            'host_id',
            metavar='<host-id>',
            help='Host to show',
        )
        return parser

    def take_action(self, parsed_args):
        host = models.Host.query.get(parsed_args.host_id)

        data = {
            'ID': host.id,
            'Name': host.name
        }

        return zip(*sorted(six.iteritems(data)))
