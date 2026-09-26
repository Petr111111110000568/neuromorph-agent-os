"""Supervise one trusted Windows localhost deployment with a kill-on-close Job.

This is process lifecycle management, not an OS sandbox. No installation,
credentials, model requests, external listeners or arbitrary commands are added.
"""
import argparse
import ctypes
from ctypes import wintypes as W
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.request

PINS = ('plugin_worker.py', 'workbench/plugins.py', 'workbench/__init__.py',
        'workbench/morphogenesis.py', 'workbench/kan.py', 'workbench/cortical.py')
PORTS = (8765, 8766, 8767)


def plain_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute_nontraversing_path_required')
    for item in (path, *path.parents):
        if item.exists() and (item.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('reparse_point_not_allowed')
    return path


def read_json(path, limit=65536):
    plain_path(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('json_size_limit')
    return json.loads(raw.decode('utf-8'))


def write_json(path, value):
    plain_path(path)
    temporary = path.with_name(path.name + '.tmp')
    plain_path(temporary)
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def validate_layout(root, expected_commit):
    root = plain_path(root)
    if not root.is_dir() or not re.fullmatch(r'[0-9a-f]{40}', expected_commit):
        raise ValueError('runtime_or_commit_invalid')
    repo, python = root / 'repo', root / 'venv/Scripts/python.exe'
    for item in (repo, repo / '.git', python, root / 'state', root / 'service'):
        plain_path(item)
    if not repo.is_dir() or not (repo / '.git').is_dir() or not python.is_file():
        raise ValueError('existing_checkout_and_venv_required')
    head = (repo / '.git/HEAD').read_text(encoding='ascii').strip()
    if head.startswith('ref: '):
        reference = head[5:]
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9_./-]+', reference) or '..' in reference:
            raise ValueError('git_reference_invalid')
        ref_path = plain_path(repo / '.git' / reference)
        if ref_path.is_file():
            head = ref_path.read_text(encoding='ascii').strip()
        else:
            packed = repo / '.git/packed-refs'
            head = next((line.split(' ', 1)[0] for line in packed.read_text(encoding='ascii').splitlines()
                         if line.endswith(' ' + reference)), '')
    if head != expected_commit:
        raise ValueError('checkout_commit_mismatch')
    pins = read_json(repo / 'data/builtin_pins.json').get('files', {})
    if type(pins) is not dict or not set(PINS).issubset(pins):
        raise ValueError('builtin_pins_missing')
    for relative, digest in pins.items():
        if type(relative) is not str or not re.fullmatch(r'[A-Za-z0-9_./-]+', relative):
            raise ValueError('builtin_pin_path_invalid')
        target = plain_path(repo / relative)
        if not target.is_relative_to(repo) or not target.is_file() or target.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('builtin_pin_path_invalid')
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('builtin_pin_mismatch')
    policy = read_json(repo / 'config/resource_policy.json')
    if policy != {'schema_version': 1, 'daily_spend_limit_usd': 0,
                  'paid_model_calls_allowed': False, 'public_catalog_reads_allowed': True}:
        raise ValueError('zero_spend_policy_required')
    (root / 'state').mkdir(exist_ok=True)
    (root / 'service').mkdir(exist_ok=True)
    return root, repo, python


def clean_environment(root):
    system = Path(os.environ['SystemRoot'])
    temporary = root / 'service/temp'
    temporary.mkdir(exist_ok=True)
    home = root / 'service/home'
    home.mkdir(exist_ok=True)
    return {'SystemRoot': str(system), 'WINDIR': str(system),
            'PATH': str(system / 'System32'), 'TEMP': str(temporary), 'TMP': str(temporary),
            'HOME': str(home), 'USERPROFILE': str(home), 'LANG': 'C.UTF-8',
            'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', W.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                 'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class JobAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ('TotalUserTime', 'TotalKernelTime',
                 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                ('TotalPageFaultCount', W.DWORD), ('TotalProcesses', W.DWORD),
                ('ActiveProcesses', W.DWORD), ('TotalTerminatedProcesses', W.DWORD)]


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR),
                ('dwX', W.DWORD), ('dwY', W.DWORD), ('dwXSize', W.DWORD), ('dwYSize', W.DWORD),
                ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD), ('dwFillAttribute', W.DWORD),
                ('dwFlags', W.DWORD), ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
                ('lpReserved2', ctypes.POINTER(W.BYTE)), ('hStdInput', W.HANDLE),
                ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]


def windows_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, W.LPCWSTR], W.HANDLE),
        'SetInformationJobObject': ([W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD], W.BOOL),
        'AssignProcessToJobObject': ([W.HANDLE, W.HANDLE], W.BOOL),
        'QueryInformationJobObject': ([W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p], W.BOOL),
        'TerminateJobObject': ([W.HANDLE, W.UINT], W.BOOL),
        'CreateProcessW': ([W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL,
                            W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)], W.BOOL),
        'ResumeThread': ([W.HANDLE], W.DWORD), 'CloseHandle': ([W.HANDLE], W.BOOL),
        'GetExitCodeProcess': ([W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
        'TerminateProcess': ([W.HANDLE, W.UINT], W.BOOL),
        'GetCurrentProcess': ([], W.HANDLE),
        'GetProcessTimes': ([W.HANDLE, ctypes.POINTER(W.FILETIME), ctypes.POINTER(W.FILETIME),
                             ctypes.POINTER(W.FILETIME), ctypes.POINTER(W.FILETIME)], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def checked(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def creation_time(kernel, handle):
    values = [W.FILETIME() for _ in range(4)]
    checked(kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)))
    ticks = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    return {'filetime': str(ticks), 'utc': datetime.fromtimestamp(ticks / 10000000 - 11644473600, timezone.utc).isoformat()}


def requested_stop(path, nonce):
    if not path.exists():
        return False
    try:
        return read_json(path, 1024) == {'schema_version': 1, 'nonce': nonce, 'operation': 'stop'}
    except (OSError, ValueError, UnicodeError):
        return False


def run(root, expected_commit, launch_id):
    import msvcrt
    if not re.fullmatch(r'[0-9a-f]{32}', launch_id):
        raise ValueError('launch_id_invalid')
    root, repo, python = validate_layout(root, expected_commit)
    service = root / 'service'
    lock_path = plain_path(service / 'owner.lock')
    with lock_path.open('a+b') as owner_lock:
        owner_lock.write(b'0'); owner_lock.flush(); owner_lock.seek(0)
        msvcrt.locking(owner_lock.fileno(), msvcrt.LK_NBLCK, 1)
        return supervise(root, repo, python, expected_commit, launch_id)


def supervise(root, repo, python, expected_commit, launch_id):
    import msvcrt
    service = root / 'service'
    receipt_path, stop_path = service / 'receipt.json', service / 'stop.json'
    for path in (receipt_path, stop_path, service / 'child.log', service / 'temp', service / 'home'):
        plain_path(path)
    for port in PORTS:
        with socket.socket() as probe:
            probe.settimeout(.2)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('required_loopback_port_in_use')
    nonce, kernel = secrets.token_hex(24), windows_api()
    job = kernel.CreateJobObjectW(None, None)
    checked(job)
    process = ProcessInfo()
    receipt = {'schema_version': 1, 'status': 'starting', 'launch_id': launch_id, 'root_pid': os.getpid(),
               'root_creation_time': creation_time(kernel, kernel.GetCurrentProcess()),
               'source_commit': expected_commit, 'repo': str(repo), 'data_dir': str(root / 'state'),
               'supervisor_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'url': 'http://127.0.0.1:8765/brain.html', 'ports': list(PORTS),
               'workers': 2, 'nonce': nonce, 'lifecycle': 'windows_job_kill_on_close',
               'is_os_sandbox': False, 'inherited_provider_credentials': False}
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        environment = clean_environment(root)
        block = ctypes.create_unicode_buffer('\0'.join(key + '=' + value for key, value in sorted(environment.items())) + '\0\0')
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(python), '-X', 'utf8', '-m', 'workbench',
                    '--data-dir', str(root / 'state'), 'local-network', '--workers', '2',
                    '--port', '8765', '--hub-port', '8766', '--federation-port', '8767']))
        with open(os.devnull, 'rb') as stdin, (service / 'child.log').open('ab', buffering=0) as output:
            input_handle, output_handle = msvcrt.get_osfhandle(stdin.fileno()), msvcrt.get_osfhandle(output.fileno())
            os.set_handle_inheritable(input_handle, True); os.set_handle_inheritable(output_handle, True)
            startup = StartupInfo()
            startup.cb, startup.dwFlags = ctypes.sizeof(startup), 0x100  # STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = input_handle, output_handle, output_handle
            checked(kernel.CreateProcessW(str(python), command, None, None, True,
                    0x4 | 0x400 | 0x08000000, block, str(repo), ctypes.byref(startup), ctypes.byref(process)))
            # Child cannot import code or spawn a worker before assignment succeeds.
            checked(kernel.AssignProcessToJobObject(job, process.hProcess))
            if kernel.ResumeThread(process.hThread) == 0xFFFFFFFF:
                checked(False)
        kernel.CloseHandle(process.hThread); process.hThread = None
        receipt['main_child_pid'] = process.dwProcessId
        receipt['main_child_creation_time'] = creation_time(kernel, process.hProcess)
        write_json(receipt_path, receipt)
        deadline, ready = time.monotonic() + 30, False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            code = W.DWORD()
            checked(kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)))
            if code.value != 259:
                receipt.update(status='child_exited', child_exit_code=code.value)
                break
            if requested_stop(stop_path, nonce):
                receipt['status'] = 'stopped'
                break
            if not ready:
                try:
                    with opener.open('http://127.0.0.1:8765/api/status', timeout=1) as response:
                        raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError('status_size_limit')
                    status = json.loads(raw.decode('utf-8'))
                    ready = (status.get('environment', {}).get('builtin_integrity') is True
                             and status.get('model_calls_enabled') is False
                             and status.get('resource_policy', {}).get('daily_spend_limit_usd') == 0)
                    if ready:
                        receipt.update(status='running', ready_at=datetime.now(timezone.utc).isoformat())
                        write_json(receipt_path, receipt)
                except (OSError, ValueError, UnicodeError):
                    pass
                if not ready and time.monotonic() >= deadline:
                    receipt['status'] = 'readiness_timeout'
                    break
            time.sleep(.5)
    except Exception as exc:
        receipt.update(status='failed', failure_kind=type(exc).__name__)
        raise
    finally:
        # Close-on-owner-exit also covers a crashed or force-killed supervisor.
        # Explicit termination permits a short wait before publishing completion.
        if process.hProcess:
            kernel.TerminateJobObject(job, 0)
            kernel.TerminateProcess(process.hProcess, 0)  # Own handle; also covers failed assignment.
            kernel.WaitForSingleObject(process.hProcess, 10000)
            deadline = time.monotonic() + 5
            accounting = JobAccounting()
            while kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                if accounting.ActiveProcesses == 0 or time.monotonic() >= deadline:
                    receipt['job_active_processes_after_stop'] = accounting.ActiveProcesses
                    break
                time.sleep(.05)
        kernel.CloseHandle(job)
        if process.hThread:
            kernel.CloseHandle(process.hThread)
        if process.hProcess:
            kernel.CloseHandle(process.hProcess)
        receipt['ended_at'] = datetime.now(timezone.utc).isoformat()
        write_json(receipt_path, receipt)
    return 0 if receipt['status'] == 'stopped' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--launch-id', required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows only')
    try:
        return run(args.runtime_root, args.source_commit, args.launch_id)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'failure_kind': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
