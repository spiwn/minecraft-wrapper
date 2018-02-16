# -*- coding: utf-8 -*-

# Copyright (C) 2017, 2018 - SurestTexas00
# This program is distributed under the terms of the GNU
# General Public License, version 3 or later.
import platform
import subprocess
import re
import os

# resource is Unix specific
try:
    import resource
except ImportError:
    resource = False

whitespaceRe = re.compile("\s+")
def _windows_memory_usage(pid=None):
    proc = subprocess.Popen(["wmic", "process", "where", "processid=" + str(pid if pid != None else os.getpid()), "get", "PeakWorkingSetSize,", "WorkingSetSize"],
            stdout=subprocess.PIPE, shell=True)
    proc.wait()
    headers = whitespaceRe.split(proc.stdout.readline().decode())
    values = whitespaceRe.split(proc.stdout.readline().decode())
    result = {}
    if (headers[0] == "PeakWorkingSetSize"):
        result["peak"] = int(values[0])
        result["rss"] = int(values[1]) // 1024
    else:
        result["peak"] = int(values[1])
        result["rss"] = int(values[0]) // 1024
    
    return result

def _unix_memory_usage(pid=None):
        result = {'peak': 0, 'rss': 0}
        try:
            """Memory usage of the current process in kilobytes."""
            # This will only work on systems with a /proc file system
            # (like Linux).
            with open('/proc/' + 'self' if (pid == None) else str(pid) + '/status') as f:
                for line in f:
                    parts = line.split()
                    key = parts[0][2:-1].lower()
                    if key in result:
                        result[key] = int(parts[1])
            return result
        except:
            if (resource == None):
                return result
            try:
                temp = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                result['peak'] = temp
                result['rss'] = temp
            except:
                pass
        return result

def _windows_get_storage_available(folder):
    """Returns the disk space for the working directory
    in bytes.
    """
    free_bytes = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(folder), None, None,
        ctypes.pointer(free_bytes))
    return free_bytes.value
    
def _unix_get_storage_available(folder):
    """Returns the disk space for the working directory
    in bytes.
    """
    st = os.statvfs(folder)
    return st.f_bavail * st.f_frsize

if platform.system() == "Windows":
    get_memory_usage = _windows_memory_usage
    get_storage_available = _windows_get_storage_available
else:
    get_memory_usage = _unix_memory_usage
    get_storage_available = _unix_get_storage_available