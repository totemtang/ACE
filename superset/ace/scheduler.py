# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import time
from threading import (Thread, Lock)

from marshmallow import ValidationError

from superset.charts.commands.exceptions import ChartDataCacheLoadError, \
    ChartDataQueryFailedError
from superset.exceptions import QueryObjectValidationError
from superset.extensions import ace_state_manager
from superset.charts.commands.data import ChartDataCommand

from superset.ace.util_class import (NodeType, START_TS, RESPONSE_CODE, RESPONSE)


class Scheduler(Thread):
    def __init__(self, dash_id: int, app):
        super().__init__()
        self.dash_id = dash_id
        self.ds_state_manager = ace_state_manager[dash_id]
        self.node_groups_list = []
        self.charts_form_data_list = []
        self.ts_list = []
        self.finished_ts_set = set()
        self.dependent_ts_set = set()
        self.scheduler_lock = Lock()
        self.finish = False
        self.app = app

    def submit_one_txn(self, ts: int, node_groups: list,
                       input_charts_form_data: dict):
        charts_form_data = {}
        for node_id_str in input_charts_form_data:
            charts_form_data[int(node_id_str)] = input_charts_form_data[node_id_str]
        self.scheduler_lock.acquire()
        self.ts_list.append(ts)
        self.node_groups_list.append(node_groups)
        self.charts_form_data_list.append(charts_form_data)
        self.scheduler_lock.release()

    def run(self):
        while not self.finish:
            self.scheduler_lock.acquire()
            if len(self.ts_list) != 0:
                cur_ts = self.ts_list.pop(0)
                cur_node_groups = self.node_groups_list.pop(0)
                cur_charts_form_data = self.charts_form_data_list.pop(0)
            else:
                cur_ts = START_TS
                cur_node_groups = None
                cur_charts_form_data = None
            self.scheduler_lock.release()
            if cur_ts != START_TS:
                cur_chart_ids = cur_node_groups[NodeType.VIZ.value - 1]
                while len(cur_chart_ids) != 0:
                    cur_chart_ids = self.skip_chart_refresh(cur_chart_ids)
                    chart_id_to_schedule = self.schedule_one_chart(cur_ts,
                                                                   cur_chart_ids)
                    self.refresh_one_chart(cur_ts, chart_id_to_schedule,
                                           cur_charts_form_data)
                    cur_chart_ids.remove(chart_id_to_schedule)
                self.finished_ts_set.add(cur_ts)
                self.dependent_ts_set.add(cur_ts)
                if self.dependent_ts_set.issubset(self.finished_ts_set):
                    max_ts = max(self.finished_ts_set)
                    self.ds_state_manager.commit_one_txn(max_ts)
                    self.finished_ts_set = set()
                    self.dependent_ts_set = set()
            time.sleep(0.01)

    def refresh_one_chart(self, ts: int, chart_id: int, charts_form_data: dict):
        with self.app.app_context():
            try:
                form_data = charts_form_data[chart_id]
                command = ChartDataCommand()
                command.set_query_context(form_data)
                # TODO: validate does not work here due to missing user attribute
                # command.validate()
                result = command.run(force_cached=False)["queries"]
                code = 200
            except QueryObjectValidationError as error:
                code = 400
                result = error.message
            except ValidationError as error:
                code = 400
                result = "Request is incorrect: {error}".format(
                    error=error.normalized_messages())
            except ChartDataCacheLoadError as exc:
                code = 400
                result = exc.message
            except ChartDataQueryFailedError as exc:
                code = 400
                result = exc.message
            except KeyError:
                code = 400
                result = "Message not follow the refresh format"

        result_dict = {
            RESPONSE_CODE: code,
            RESPONSE: result
        }

        self.ds_state_manager.finish_one_update(chart_id, ts, result_dict)

    def schedule_one_chart(self, ts: int, cur_chart_ids: set) -> int:
        return self.ds_state_manager.get_top_priority_node(ts, cur_chart_ids)

    def skip_chart_refresh(self, cur_chart_ids: set) -> set:
        new_chart_ids = set(cur_chart_ids)
        self.scheduler_lock.acquire()
        for i in range(len(self.node_groups_list)):
            pending_chart_ids = self.node_groups_list[i][NodeType.VIZ.value - 1]
            pending_ts = self.ts_list[i]
            for chart_id in cur_chart_ids:
                if chart_id in pending_chart_ids:
                    new_chart_ids.remove(chart_id)
                    self.dependent_ts_set.add(pending_ts)
        self.scheduler_lock.release()
        return new_chart_ids

    def shut_down(self) -> None:
        self.finish = True
