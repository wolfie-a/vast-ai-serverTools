import subprocess
import re
import os
import sys
import time
from datetime import datetime

CHECK_INTERVAL = 30 
LOG_FILE = "aer_thermal_log.txt"

error_counters = {}
max_temps = {}

def get_rig_inventory():
    inventory = {'GPU': {}, 'NVMe': {}, 'NIC': {}}
    # --- 1. GPUs ---
    if subprocess.run(["command", "-v", "nvidia-smi"], capture_output=True, shell=True).returncode == 0:
        try:
            cmd = ["nvidia-smi", "--query-gpu=pci.bus_id,name,serial,uuid", "--format=csv,noheader,nounits"]
            smi_out = subprocess.check_output(cmd, text=True)
            for line in smi_out.strip().split('\n'):
                bus, name, sn, uuid = [x.strip() for x in line.split(',')]
                full_bdf = bus.lower()
                short_bdf = ":".join(full_bdf.split(':')[-2:])
                lspci_out = subprocess.check_output(f"lspci -s {full_bdf} -vv", shell=True, text=True)
                subs = re.search(r"Subsystem: (.*)", lspci_out)
                vendor = subs.group(1).split("Device")[0].strip() if subs else ""
                final_name = f"{name} — {vendor}" if vendor else name
                serial = sn if (sn and sn != "[Not Supported]" and sn != "0") else uuid
                inventory['GPU'][short_bdf] = {'name': final_name, 'sn': serial, 'full_bdf': full_bdf}
        except: pass

    # --- 2. NVMe ---
    if subprocess.run(["command", "-v", "nvme"], capture_output=True, shell=True).returncode == 0:
        try:
            nvme_info = {}
            list_out = subprocess.check_output(["nvme", "list"], text=True)
            for line in list_out.strip().split('\n'):
                if line.startswith('/dev/nvme'):
                    p = re.split(r'\s{2,}', line)
                    if len(p) >= 3: nvme_info[p[1]] = p[2]
            for ctrl in os.listdir('/sys/class/nvme'):
                try:
                    pci_path = os.path.realpath(f"/sys/class/nvme/{ctrl}/device")
                    short_bdf = ":".join(os.path.basename(pci_path).split(':')[-2:])
                    with open(f"/sys/class/nvme/{ctrl}/serial", 'r') as f:
                        sn = f.read().strip()
                    model = nvme_info.get(sn, "Unknown NVMe")
                    inventory['NVMe'][short_bdf] = {'ctrl': ctrl, 'model': model, 'sn': sn}
                except: continue
        except: pass

    # --- 3. NICs ---
    try:
        for net_dev in os.listdir('/sys/class/net'):
            pci_link = f"/sys/class/net/{net_dev}/device"
            if os.path.exists(pci_link):
                pci_addr = os.path.basename(os.readlink(pci_link))
                short_bdf = ":".join(pci_addr.split(':')[-2:])
                with open(f"/sys/class/net/{net_dev}/address", 'r') as f:
                    mac = f.read().strip()
                nic_desc = subprocess.check_output(f"lspci -s {pci_addr}", shell=True, text=True).split(':', 2)[-1].strip()
                inventory['NIC'][short_bdf] = {'desc': nic_desc, 'iface': net_dev, 'mac': mac}
    except: pass
    return inventory

def get_nvme_temp(bdf):
    try:
        for ctrl in os.listdir('/sys/class/nvme'):
            if bdf in os.readlink(f'/sys/class/nvme/{ctrl}/device'):
                out = subprocess.check_output(["nvme", "smart-log", f"/dev/{ctrl}"], text=True)
                temp_match = re.search(r"temperature\s+:\s+(\d+)", out)
                if temp_match:
                    val = int(temp_match.group(1))
                    if bdf not in max_temps or val > max_temps[bdf]: max_temps[bdf] = val
                    return val
    except: return None

def get_gpu_temp(full_bdf, short_bdf):
    try:
        cmd = ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits", f"--id={full_bdf}"]
        val = int(subprocess.check_output(cmd, text=True).strip())
        if short_bdf not in max_temps or val > max_temps[short_bdf]: max_temps[short_bdf] = val
        return val
    except: return None

def color_len(text):
    return len(re.sub(r'\033\[[0-9;]*m', '', text))

def pad_colored(text, width):
    return text + (" " * max(0, width - color_len(text)))

# Initial Discovery
RIG_MAP = get_rig_inventory()
seen_entries = set()

while True:
    try:
        raw_logs = subprocess.check_output(["dmesg"], text=True)
        for line in raw_logs.strip().split('\n')[-150:]:
            if "AER:" in line and line not in seen_entries:
                seen_entries.add(line)
                match = re.search(r"([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])", line)
                if match:
                    short_bdf = ":".join(match.group(1).split(':')[-2:])
                    error_counters[short_bdf] = error_counters.get(short_bdf, 0) + 1

        print(f"\033[2J\033[H")
        print(f"--- RIG HEALTH MONITOR | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

        # --- NVMe SECTION ---
        print(f"\n[ NVMe STORAGE ]")
        print(f"{'PCIe':<8} | {'#AER ERRS':<9} | {'TEMP':<7} | {'MAX':<7} | {'MODEL':<30} | {'SERIAL'}")
        print("-" * 125)
        for bdf, info in RIG_MAP['NVMe'].items():
            errs = error_counters.get(bdf, 0)
            cur_t = get_nvme_temp(bdf)
            max_t = max_temps.get(bdf, "---")
            t_str = f"\033[93m{cur_t} C\033[0m" if cur_t else "---"
            m_str = f"\033[91m{max_t} C\033[0m" if (isinstance(max_t, int) and max_t > 75) else f"{max_t} C" if max_t != "---" else "---"
            bdf_c = f"\033[91m{bdf}\033[0m" if errs > 0 else f"\033[92m{bdf}\033[0m"
            print(f"{pad_colored(bdf_c, 8)} | {errs:<9} | {pad_colored(t_str, 7)} | {pad_colored(m_str, 7)} | {info['model']:<30} | {info['sn']}")

        # --- GPU SECTION ---
        print(f"\n[ GRAPHICS CARDS ]")
        print(f"{'PCIe':<8} | {'#AER ERRS':<9} | {'TEMP':<7} | {'MAX':<7} | {'NAME & MANUFACTURER':<65} | {'SERIAL'}")
        print("-" * 145)
        for bdf, info in RIG_MAP['GPU'].items():
            errs = error_counters.get(bdf, 0)
            cur_t = get_gpu_temp(info['full_bdf'], bdf)
            max_t = max_temps.get(bdf, "---")
            t_str = f"\033[93m{cur_t} C\033[0m" if cur_t else "---"
            m_str = f"\033[91m{max_t} C\033[0m" if (isinstance(max_t, int) and max_t > 85) else f"{max_t} C" if max_t != "---" else "---"
            bdf_c = f"\033[91m{bdf}\033[0m" if errs > 0 else f"\033[92m{bdf}\033[0m"
            print(f"{pad_colored(bdf_c, 8)} | {errs:<9} | {pad_colored(t_str, 7)} | {pad_colored(m_str, 7)} | {info['name']:<65} | {info['sn']}")

        # --- NIC SECTION ---
        # Fixed CHIPSET to 110 to handle the full Broadcom description string
        print(f"\n[ NETWORK INTERFACES ]")
        print(f"{'PCIe':<8} | {'#AER ERRS':<9} | {'INTERFACE':<16} | {'CHIPSET':<110} | {'MAC ADDRESS'}")
        print("-" * 175)
        for bdf, info in RIG_MAP['NIC'].items():
            errs = error_counters.get(bdf, 0)
            bdf_c = f"\033[91m{bdf}\033[0m" if errs > 0 else f"\033[92m{bdf}\033[0m"
            print(f"{pad_colored(bdf_c, 8)} | {errs:<9} | {info['iface']:<16} | {info['desc']:<110} | {info['mac']}")

        sys.stdout.flush()
    except Exception as e:
        print(f"\n[ERROR] {e}")
    time.sleep(CHECK_INTERVAL)
