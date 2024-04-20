# Stdlib
import os
from typing import Mapping
import string
# External packages
import toml

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
        self.device_startup = {}
        self.topoid_devices = {}
        self.net_ids = {}
        self.next_net_id = "0"
        self.alphabet = string.digits + string.ascii_lowercase

        self.if_name = "eth" if self.args.kathara else "net"
        self.output_base = os.environ.get('SCION_OUTPUT_BASE', os.getcwd())
        self.lab_dir = str(os.path.join(self.args.output_dir, KATHARA_GEN_PATH))
        self.config_dir = "etc/scion"

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
        # self._patch_sd_config()
        self._write_lab()

    def _initiate_lab(self):
        self.lab_conf += f'LAB_DESCRIPTION="SCION on Kathará Lab from topology {self.args.topo_config}"\n'
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
                    if dev_id not in self.device_startup:
                        self.device_startup[dev_id] = {
                            "content": "",
                            "is_ipv6": False,
                            "ip": ip,
                        }
                    else:
                        # Replace the tester_ IP address with the SD IP address
                        self.device_startup[dev_id]["ip"] = ip
                else:
                    dev_id = dev_id.replace("tester_", "sd")
                    # Force the tester to use the same interface as the SD
                    if dev_id not in self.devices_ifids:
                        self.devices_ifids[dev_id] = -1
                    else:
                        self.devices_ifids[dev_id] -= 1

                    if dev_id not in self.device_startup:
                        self.device_startup[dev_id] = {
                            "content": "",
                            "is_ipv6": False,
                            "ip": ip,
                        }

                ifid = self.devices_ifids[dev_id] if self.devices_ifids[dev_id] >= 0 else 0
                # Add IP addresses to startup script
                if ip.version == 4:
                    self.device_startup[dev_id][
                        "content"] += f'ip addr add {ip} dev {self.if_name}{ifid}\n'
                else:
                    self.device_startup[dev_id][
                        "content"] += f'ip -6 addr add {ip} dev {self.if_name}{ifid}\n'
                    # Add ipv6 to lab.conf
                    if not self.device_startup[dev_id]["is_ipv6"]:
                        gen_lines.append(f'{dev_id}[ipv6]=True\n')
                        self.device_startup[dev_id]["is_ipv6"] = True

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
                    image = docker_image(self.args, 'posix-router')
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
                self.device_startup[dev_id]["content"] += "sleep 2s\n"

                if dev_id.startswith("br"):
                    self.device_startup[dev_id]["content"] += f'/app/router --config /{self.config_dir}/br.toml &\n'
                elif dev_id.startswith("cs"):
                    self.device_startup[dev_id]["content"] += f'/app/control --config /{self.config_dir}/cs.toml &\n'
                elif dev_id.startswith("sd"):
                    self.device_startup[dev_id]["content"] += f'/app/daemon --config /{self.config_dir}/sd.toml &\n'

    def _add_enviroment_variables(self):
        for topo_id, _ in self.args.topo_dicts.items():
            conf_dir = str(os.path.join(self.output_base, topo_id.base_dir(self.args.output_dir)))
            sd_toml = os.path.join(conf_dir, "sd.toml")
            sd_dev_id = sciond_name(topo_id).replace("-", "_")
            # Read SD config
            with open(sd_toml, "r") as f:
                sd_config = toml.load(f)
                self.device_startup[sd_dev_id]["content"] += f'echo \'export SCION_DAEMON="{sd_config["sd"]["address"]}"\' >> /root/.bashrc \n'       
                
    def _replace_string(self, obj, original_value, replace_value):
        for key, value in obj.items():
            if isinstance(value, dict):
                self._replace_string(value, original_value, replace_value)
            elif isinstance(value, str):
                obj[key] = value.replace(original_value, replace_value)


    def _write_lab(self):
        write_file(os.path.join(self.lab_dir, KATHARA_LAB_CONF), self.lab_conf)
        for dev_id, content in self.device_startup.items():
            write_file(os.path.join(self.lab_dir, f"{dev_id}.startup"), content["content"])

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
                symlink(f"{conf_dir}/prometheus", f"{dest_conf_dir}/prometheus", is_dir=True)
                symlink(f"{conf_dir}/topology.json", f"{dest_conf_dir}/topology.json")
                symlink(f"{conf_dir}/prometheus.yml", f"{dest_conf_dir}/prometheus.yml")

                if dev_id.startswith("sd"):
                    symlink(f"{conf_dir}/sd.toml", f"{dest_conf_dir}/sd.toml")
                else:
                    real_dev_id = self.get_real_device_id(dev_id)
                    symlink(f"{conf_dir}/{real_dev_id}.toml", f"{dest_conf_dir}/{real_dev_id[:2]}.toml")

    