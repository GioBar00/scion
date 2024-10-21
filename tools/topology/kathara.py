# Stdlib
import os
from typing import Mapping
import string
# External packages
import toml
import json

from topology.defines import KATHARA_GEN_PATH
# SCION
from topology.util import write_file, symlink
from topology.common import (
    ArgsTopoDicts,
    docker_image,
    sciond_name,
)
from topology.net import NetworkDescription, IPNetwork

KATHARA_LAB_CONF = 'lab.conf'
SCMP_PATH_PROBE_TARGETS_FILE = "scmp_path_probe_targets.json"
SCMP_PATH_PROBE_SCRIPT_FILE = "scmp_path_probe.py"
CRON_SCRIPT_FILE = "cron.sh"
FULL_CONN_SH = "full_conn.sh"


class KatharaLabGenArgs(ArgsTopoDicts):
    def __init__(self, args, topo_dicts,
                 networks: Mapping[IPNetwork, NetworkDescription]):
        """
        :param object args: Contains the passed command line arguments as named attributes.
        :param dict topo_dicts: The generated topo dicts from TopoGenerator.
        :param dict networks: The generated networks from SubnetGenerator.
        """
        super().__init__(args, topo_dicts)
        self.networks = networks


class KatharaLabGenerator(object):

    def __init__(self, args):
        """
        :param KatharaLabGenArgs args: Contains the passed command line arguments and topo dicts.
        """
        self.args = args
        self.lab_conf = ""
        self.devices_ifids = {}
        self.device_info = {}
        self.topoid_devices = {}
        self.net_ids = {}
        self.next_net_id = "0"
        self.alphabet = string.digits + string.ascii_lowercase

        self.if_name = "net"
        self.output_base = os.environ.get('SCION_OUTPUT_BASE', os.getcwd())
        self.lab_dir = str(os.path.join(self.args.output_dir, KATHARA_GEN_PATH))
        self.config_dir = "etc/scion"

        self._init_file_content()

    def get_real_device_id(self, dev_id):
        real_dev_id = dev_id.replace("_", "-", 1)
        idx = real_dev_id.rfind("_")
        if idx != -1:
            real_dev_id = real_dev_id[:idx] + "-" + real_dev_id[idx + 1:]
        return real_dev_id

    def _increment_net_id(self, idx):
        if idx < 0:
            self.next_net_id = self.alphabet[0] + self.next_net_id
        elif self.next_net_id[idx] == self.alphabet[-1]:
            self.next_net_id = self.next_net_id[:idx] + self.alphabet[0] + self.next_net_id[idx + 1:]
            self._increment_net_id(idx - 1)
        else:
            self.next_net_id = self.next_net_id[:idx] + self.alphabet[self.alphabet.index(self.next_net_id[idx]) + 1] + self.next_net_id[idx + 1:]

    def generate_lab(self):
        self._initiate_lab()
        self._assign_networks()
        self._add_container_images()
        self._add_enviroment_variables()
        self._add_commands()
        self._patch_monitoring_config()
        self._expose_paths_metrics()
        self._create_full_conn_script()
        self._write_lab()

    def _initiate_lab(self):
        self.lab_conf += f'LAB_DESCRIPTION="MEGASCION -- SCION on Kathará Lab from topology {str(self.args.topo_config).split("/")[-1]}"\n'
        self.lab_conf += f'LAB_AUTHOR="ETH Zurich"\n'
        self.lab_conf += f'LAB_VERSION=1.0\n'
        self.lab_conf += f'LAB_WEB="https://github.com/scionproto/scion"\n'
        self.lab_conf += '\n'

        for topo_id, topo in self.args.topo_dicts.items():
            self.topoid_devices[topo_id] = []
            for dev_id in topo.get("border_routers", {}).keys() | topo.get("control_service", {}).keys():
                self.topoid_devices[topo_id].append(dev_id.replace("-", "_"))

            self.topoid_devices[topo_id].append(sciond_name(topo_id).replace("-", "_"))

    def _assign_networks(self):
        self.lab_conf += '# Collision domains\n'
        gen_lines = []
        for net, desc in self.args.networks.items():
            if net not in self.net_ids:
                self.net_ids[net] = self.next_net_id
                self._increment_net_id(len(self.next_net_id) - 1)
            coll_domain = f"{self.net_ids[net]}"
            for dev_id, ip in desc.ip_net.items():
                dev_id = dev_id.replace("_internal", "").replace("-", "_")

                if not dev_id.startswith("tester_"):
                    if dev_id not in self.devices_ifids:
                        self.devices_ifids[dev_id] = 0
                    # Add collision domain to lab.conf
                    gen_lines.append(f'{dev_id}[{self.devices_ifids[dev_id]}]="{coll_domain}"\n')
                    if dev_id not in self.device_info:
                        self.device_info[dev_id] = {
                            "startup": "",
                            "shutdown": "",
                            "is_ipv6": False,
                            "ip": ip,
                        }
                    else:
                        # Replace the tester_ IP address with the SD IP address
                        self.device_info[dev_id]["ip"] = ip
                else:
                    dev_id = dev_id.replace("tester_", "sd")
                    # Force the tester to use the same interface as the SD
                    if dev_id not in self.devices_ifids:
                        self.devices_ifids[dev_id] = -1
                    else:
                        self.devices_ifids[dev_id] -= 1

                    if dev_id not in self.device_info:
                        self.device_info[dev_id] = {
                            "startup": "",
                            "shutdown": "",
                            "is_ipv6": False,
                            "ip": ip,
                        }

                ifid = self.devices_ifids[dev_id] if self.devices_ifids[dev_id] >= 0 else 0
                # Add IP addresses to startup script
                if ip.version == 4:
                    self.device_info[dev_id][
                        "startup"] += f'ip addr add {ip} dev {self.if_name}{ifid}\n'
                else:
                    self.device_info[dev_id][
                        "startup"] += f'ip -6 addr add {ip} dev {self.if_name}{ifid}\n'

                self.devices_ifids[dev_id] += 1

        gen_lines.sort()
        for line in gen_lines:
            self.lab_conf += line
        self.lab_conf += '\n'

    def _add_container_images(self):
        self.lab_conf += '# Container images\n'
        gen_lines = []
        for topo_id, _ in self.args.topo_dicts.items():
            for dev_id in self.topoid_devices[topo_id]:
                if dev_id.startswith("br"):
                    image = docker_image(self.args, 'router')
                elif dev_id.startswith("cs"):
                    image = docker_image(self.args, 'control')
                elif dev_id.startswith("sd"):
                    image = docker_image(self.args, 'endhost')
                else:
                    continue
                gen_lines.append(f'{dev_id}[image]="{image}"\n')

        gen_lines.sort()
        for line in gen_lines:
            self.lab_conf += line
        self.lab_conf += '\n'

    def _add_commands(self):
        for topo_id, _ in self.args.topo_dicts.items():
            for dev_id in self.topoid_devices[topo_id]:
                # Added to fix bind error with IPv6
                self.device_info[dev_id]["startup"] += "sleep 2s\n"

                # if self.args.megalos:
                #     # Add default route to eth0 to allow prometheus to scrape the metrics
                #     self.device_startup[dev_id]["startup"] += "ip route add 100.64.0.0/10 dev eth0\n"

                self.device_info[dev_id]["startup"] += f'ntpd\n'

                if dev_id.startswith("br"):
                    self.device_info[dev_id]["startup"] += f'/app/router --config /{self.config_dir}/br.toml &\n'
                    continue
                elif dev_id.startswith("cs"):
                    #self.device_info[dev_id]["startup"] += f'/app/control --config /{self.config_dir}/cs.toml &\n'
                    continue
                elif dev_id.startswith("sd"):
                    self.device_info[dev_id]["startup"] += f'/app/daemon --config /{self.config_dir}/sd.toml &\n'
                    #self.device_info[dev_id]["startup"] += f'chmod +x /{self.config_dir}/{CRON_SCRIPT_FILE} &\n'
                    self.device_info[dev_id]["startup"] += f'chmod +x /{self.config_dir}/{FULL_CONN_SH} &\n'
                    #self.device_info[dev_id]["startup"] += "sleep 2s\n"
                    # self.device_info[dev_id]["startup"] += f'/{self.config_dir}/{CRON_SCRIPT_FILE} &\n'
                    #self.device_info[dev_id]["startup"] += f'/{self.config_dir}/{FULL_CONN_SH} &\n'

                    # Add shutdown commands: Clean scmp_path logs from shared folder
                    # self.device_info[dev_id]["shutdown"] += f'pkill -f {CRON_SCRIPT_FILE}\n'
                    # self.device_info[dev_id]["shutdown"] += f'bash -l -c "rm -f /shared/$(hostname).prom"\n'
                    continue

    def _add_enviroment_variables(self):
        for topo_id, _ in self.args.topo_dicts.items():
            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            sd_toml = os.path.join(conf_dir, "sd.toml")
            sd_dev_id = sciond_name(topo_id).replace("-", "_")
            # Read SD config
            with open(sd_toml, "r") as f:
                sd_config = toml.load(f)
                self.device_info[sd_dev_id]["startup"] += f'echo \'export SCION_DAEMON="{sd_config["sd"]["address"]}"\' >> /root/.bashrc \n'

    def _patch_monitoring_config(self):
        for topo_id, _ in self.args.topo_dicts.items():

            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            for dev_id in self.topoid_devices[topo_id]:
                if dev_id.startswith("sd"):
                    conf_toml = f"{conf_dir}/sd.toml"
                else:
                    real_dev_id = self.get_real_device_id(dev_id)
                    conf_toml = f"{conf_dir}/{real_dev_id}.toml"

                with open(conf_toml, "r+") as f:
                    conf = toml.load(f)
                    conf["metrics"]["prometheus"] = "0.0.0.0:" + str(conf["metrics"]["prometheus"]).split(":")[-1]
                    if "tracing" in conf:
                        # conf["tracing"]["agent"] = "jaeger-all-in-one.monitoring.svc.cluster.local:" + str(conf["tracing"]["agent"]).split(":")[1]
                        conf["tracing"]["enabled"] = False
                    if dev_id.startswith("sd"):
                        conf["sd"]["disable_seg_verification"] = True
                    f.seek(0)
                    f.write(toml.dumps(conf))
                    f.truncate()

    def _create_full_conn_script(self):
        scionds = {}
        for topo_id, _ in self.args.topo_dicts.items():
            for dev_id in self.topoid_devices[topo_id]:
                if dev_id.startswith("sd"):
                    ip = self.device_info[dev_id]["ip"]
                    scionds[topo_id] = ip.ip
                    break
        for topo_id, remote in scionds.items():
            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            others = [t for t in scionds.keys() if t != topo_id]
            server_port = 64646
            full_conn_sh = f"""#!/bin/bash
output_file=fc-$(hostname).txt
# Full connectivity test script
date --rfc-3339=seconds >> $output_file

# Function to start the server and monitor output
start_server() {{
  while : ; do
    # Start the server in the background
    bash -l -c "./bin/end2end -mode server -local [{topo_id},{scionds[topo_id]}]:{str(server_port)}" > server_output.log 2>&1 &
    SERVER_PID=$!

    # Monitor the log to check if "Listening" is on the third line
    while : ; do
      if [[ $(sed -n '3p' server_output.log) == *"Listening"* ]]; then
        return 0  # Exit function when the server starts successfully
      fi

      # If server process is not running, break the loop and try again
      if ! kill -0 $SERVER_PID 2>/dev/null; then
        break
      fi
    done
  done
}}

# Call the start_server function and wait until it succeeds
start_server

TARGET_PONGS={len(others)}
PONG_COUNT=0

append_to_file() {{
    local data=$1

    # Use flock to prevent concurrent writes to the file
    (flock -x 200
        echo "$data" >> $output_file
    ) 200>>$output_file
}}


# Function to run the client command with the provided remote address, retrying if ERROR is found
function run_client {{
    local remote_ia=$1
    local remote_address=$2
    local client_port=$3
    while true; do
        OUTPUT=$(bash -l -c "./bin/end2end -mode client -local [{topo_id},{scionds[topo_id]}]:$client_port -remote [${{remote_ia}},${{remote_address}}]:{str(server_port)} -timeout 1s" 2>&1)
        # Append the output to the a log file
        echo "$OUTPUT" >> client${{client_port}}_output.log
        if echo "$OUTPUT" | grep -q "Received pong"; then
            append_to_file "${{remote_ia}}_$(date --rfc-3339=seconds)"
            break
        fi
    done
}}

# Function to monitor server output for "Sent pong to"
function monitor_server {{
    while [ "$PONG_COUNT" -lt "$TARGET_PONGS" ]; do
        # Read the entire server output log and count occurrences of "Sent pong to"
        PONG_COUNT=$(grep -o "Sent pong to" server_output.log | wc -l)

        # Sleep before rechecking
        sleep 1
    done
}}

# Run the server monitoring in the background
monitor_server &
MONITOR_PID=$!

"""
            for i, other in enumerate(others):
                remote = scionds[other]
                full_conn_sh += f"""run_client "{other}" "{remote}" "{server_port - i - 1}" &
PID{i}=$!
"""
            # Wait for all clients to finish
            full_conn_sh += 'wait '
            for i in range(len(others)):
                full_conn_sh += f'$PID{i} '
            full_conn_sh += '\n'

            full_conn_sh += """
# Output the completion time in RFC3339 format
date --rfc-3339=seconds >> $output_file

# Wait for the monitor server function to finish
wait $MONITOR_PID

# Sleep for 5 minutes before killing the server
sleep 5m

# Kill the server
kill $SERVER_PID
"""

            write_file(os.path.join(conf_dir, FULL_CONN_SH), full_conn_sh)


    def _expose_paths_metrics(self):
        write_file(os.path.join(os.path.join(self.output_base, self.args.output_dir), CRON_SCRIPT_FILE), self._cron_content)
        write_file(os.path.join(os.path.join(self.output_base, self.args.output_dir), SCMP_PATH_PROBE_SCRIPT_FILE), self._scmp_path_probe_content)

        for topo_id, _ in self.args.topo_dicts.items():
            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            scmp_path_probe_targets_json = [str(t) for t, _ in self.args.topo_dicts.items() if t != topo_id]
            write_file(os.path.join(conf_dir, SCMP_PATH_PROBE_TARGETS_FILE), json.dumps(scmp_path_probe_targets_json))


    def _replace_string(self, obj, original_value, replace_value):
        for key, value in obj.items():
            if isinstance(value, dict):
                self._replace_string(value, original_value, replace_value)
            elif isinstance(value, str):
                obj[key] = value.replace(original_value, replace_value)


    def _write_lab(self):
        write_file(os.path.join(self.lab_dir, KATHARA_LAB_CONF), self.lab_conf)
        for dev_id, info in self.device_info.items():
            write_file(os.path.join(self.lab_dir, f"{dev_id}.startup"), info["startup"])
            if info["shutdown"]:
                write_file(os.path.join(self.lab_dir, f"{dev_id}.shutdown"), info["shutdown"])

        self._create_directory_structure()

    def _create_directory_structure(self):
        for topo_id, _ in self.args.topo_dicts.items():
            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            for dev_id in self.topoid_devices[topo_id]:
                dest_dev_dir = os.path.join(self.output_base, os.path.join(self.lab_dir, dev_id))
                dest_conf_dir = os.path.join(dest_dev_dir, self.config_dir)
                os.makedirs(dest_conf_dir)

                symlink(f"{conf_dir}/certs", f"{dest_conf_dir}/certs", is_dir=True)
                symlink(f"{conf_dir}/crypto", f"{dest_conf_dir}/crypto", is_dir=True)
                symlink(f"{conf_dir}/keys", f"{dest_conf_dir}/keys", is_dir=True)
                symlink(f"{conf_dir}/topology.json", f"{dest_conf_dir}/topology.json")

                if dev_id.startswith("sd"):
                    symlink(f"{conf_dir}/sd.toml", f"{dest_conf_dir}/sd.toml")
                    symlink(f"{conf_dir}/{SCMP_PATH_PROBE_TARGETS_FILE}", f"{dest_conf_dir}/{SCMP_PATH_PROBE_TARGETS_FILE}")
                    symlink(f"{str(os.path.join(self.output_base, self.args.output_dir))}/{CRON_SCRIPT_FILE}", f"{dest_conf_dir}/{CRON_SCRIPT_FILE}")
                    symlink(f"{str(os.path.join(self.output_base, self.args.output_dir))}/{SCMP_PATH_PROBE_SCRIPT_FILE}", f"{dest_conf_dir}/{SCMP_PATH_PROBE_SCRIPT_FILE}")
                    symlink(f"{conf_dir}/{FULL_CONN_SH}", f"{dest_conf_dir}/{FULL_CONN_SH}")
                else:
                    real_dev_id = self.get_real_device_id(dev_id)
                    symlink(f"{conf_dir}/{real_dev_id}.toml", f"{dest_conf_dir}/{real_dev_id[:2]}.toml")


    def _init_file_content(self):
        self._cron_content = """#!/bin/bash

PATH_METRICS_FILE=/shared/$(hostname).prom
touch "$PATH_METRICS_FILE"

trap "rm -f $PATH_METRICS_FILE" EXIT

while true; do
    bash -l -c "python3 /etc/scion/scmp_path_probe.py" > "$PATH_METRICS_FILE"
    sleep 30s
done"""

        self._scmp_path_probe_content = """#!/usr/bin/env python3
# Copyright 2021 ETH Zurich
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import pathlib
import subprocess

# File to load targets for probing
targets_file = '/etc/scion/scmp_path_probe_targets.json'
# AS topo file path
topo_path = '/etc/scion/topology.json'


def main():
    local_ia = json.loads(pathlib.Path(topo_path).read_text())['isd_as']
    probe_targets = load_targets()
    # Metric header
    print("# HELP scionlab_scion_paths Number of SCION paths to destination AS")
    print("# TYPE scionlab_scion_paths gauge")
    for target_ia in probe_targets:
        try:
            raw_result = probe(target_ia)
        except subprocess.CalledProcessError:
            # Ignore failures, since we only count successful queries
            continue
        try:
            result = json.loads(raw_result.stdout)
            output_metrics(local_ia, target_ia, result)
        except json.JSONDecodeError:
            # invalid result
            continue
    return


def probe(target):
    return subprocess.run(['./bin/scion', 'showpaths', target, '--format=json'],
                          stdout=subprocess.PIPE, encoding='utf-8', check=True)


def load_targets():
    return json.loads(pathlib.Path(targets_file).read_text())


def output_metrics(local_ia, target_ia, results):
    # Count all alive paths for current src-dst AS pair
    paths = results.get('paths', [])
    alive_paths = sum(1 for p in paths if p.get('status', None) == 'alive')
    dead_paths = len(paths) - alive_paths

    isd, as_ = local_ia.split('-')
    dst_isd, dst_as = target_ia.split('-')
    base_labels = {
        "isd": isd,
        "as": as_,
        "dst_isd": dst_isd,
        "dst_as": dst_as,
    }
    print(fmt_metric("scionlab_scion_paths", {
          **base_labels, "status": "alive"}, alive_paths))
    print(fmt_metric("scionlab_scion_paths", {
          **base_labels, "status": "dead"}, dead_paths))


def fmt_metric(metric, labels, value):
    labels_fmted = ",".join("%s=\\"%s\\"" % (k, v) for k, v in labels.items())
    return "%s{%s} %s" % (metric, labels_fmted, value)


if __name__ == "__main__":
    main()
"""
