import requests
import re
from itertools import islice

symbolication_api_url = 'https://symbols.mozilla.org/symbolicate/v5'

collapse_arguments = True
escape_single_quote = True
maximum_frames_to_consider = 40
signature_max_len = 255

# extract function name from "fn (in module)"
extract_function_name = re.compile(r'\A(.+) (\(in .+\))\Z')

hex_addr = re.compile(r'\A0x[0-9a-fA-F]+\Z')

empty_request = {'memoryMap': [], 'stacks': [], 'version': 5}


def get_symbolication_request(stack_traces):
    """Take stack trace and return body of request to Symbols API"""
    # make sure we have threads, modules, and crashing_thread
    missing = ''
    if 'threads' not in stack_traces:
        missing = 'threads'
    elif 'modules' not in stack_traces:
        missing = 'modules'
    elif not stack_traces.get('crash_info', None):
        missing = 'crash_info'
    else:
        threads = stack_traces['threads']
        modules = stack_traces['modules']
        if 'crashing_thread' not in stack_traces['crash_info']:
            missing = 'crashing_thread'
        else:
            crashing_thread = stack_traces['crash_info']['crashing_thread']

    if missing:
        msg = "missing " + missing
        if stack_traces:
            msg += "; " + stack_traces.get('status', 'STATUS MISSING')
        raise ValueError(msg)

    if not (crashing_thread >= 0 and crashing_thread < len(threads)):
        msg = "crashing_thread " + str(crashing_thread)
        msg += " out of range"
        raise ValueError(msg)

    modules_to_symbolicate = []
    threads_to_symbolicate = []

    for thread_idx, src_thread in enumerate(threads):
        frames_to_symbolicate = []

        # only the crashing thread and thread 0 are used for the
        # signature, skip symbol lookup for others
        if thread_idx != 0 and thread_idx != crashing_thread:
            continue

        if 'frames' not in src_thread:
            continue

        for frame_idx, src_frame in enumerate(islice(
                src_thread['frames'], maximum_frames_to_consider)):
            out_frame = {}

            if 'ip' not in src_frame:
                msg = "missing ip for thread " + thread_idx + " frame "
                msg += frame_idx
                raise ValueError(msg)

            ip_int = int(src_frame['ip'], 16)
            out_frame['offset'] = src_frame['ip']

            if 'module_index' not in src_frame:
                continue

            module_index = src_frame['module_index']
            if not (module_index >= 0 and module_index < len(modules)):
                msg = "module_index " + module_index + " out of range for "
                msg += "thread " + thread_idx + " frame " + frame_idx
                raise ValueError(msg)

            module = modules[module_index]

            if 'base_addr' not in module:
                msg = "missing base_addr for module " + module_index
                raise ValueError(msg)

            try:
                module_offset_int = ip_int - int(module['base_addr'], 16)
            except ValueError:
                msg = "bad base_addr " + module['base_addr']
                msg += "for module " + module_index
                raise ValueError(msg)

            if 'filename' in module:
                out_frame['module'] = module['filename']
            out_frame['module_offset'] = '0x%x' % module_offset_int

            # prepare this frame for symbol lookup

            if 'debug_file' in module and 'debug_id' in module:
                mp = (module['debug_file'], module['debug_id'])
                if mp not in modules_to_symbolicate:
                    modules_to_symbolicate.append(mp)

                frames_to_symbolicate.append(
                    {'lookup': [modules_to_symbolicate.index(mp),
                                module_offset_int],
                     'output': out_frame})

        if len(frames_to_symbolicate) > 0:
            threads_to_symbolicate.append(frames_to_symbolicate)

    if len(threads_to_symbolicate) == 0:
        return empty_request

    sym_request = {
        'stacks': [[f['lookup'] for f in t] for t in threads_to_symbolicate],
        'memoryMap':
            [[debug_file, debug_id] for
             (debug_file, debug_id) in modules_to_symbolicate],
        'version': 5}

    return sym_request


def get_symbolicated_trace(sym_request):
    response = requests.post(symbolication_api_url,
                             json=sym_request)
    response.raise_for_status()
    sym_result = response.json()

    return sym_result


def try_get_sym_req(trace):
    try:
        return get_symbolication_request(trace)
    except ValueError:
        return empty_request


def symbolicate(trace):
    """Symbolicate a single crash trace

    :param dict trace: raw crash trace
    :return: symbolicated trace
    """
    return symbolicate_multi([trace])[0]


def symbolicate_multi(traces):
    """Symbolicate a list of crash traces

    :param list traces: list of raw crash traces
    :return: list of symbolicated traces
    """
    symbolication_requests = {
        'jobs': [try_get_sym_req(t) for t in traces]
    }
    crashing_threads = [t['crash_info'].get('crashing_thread', 0) for
                        t in traces]

    symbolicated_list = get_symbolicated_trace(symbolication_requests)

    # make into siggen suitable format
    formatted_symbolications = []
    for result, crashing_thread in zip(symbolicated_list['results'],
                                       crashing_threads):
        symbolicated = {'crashing_thread': crashing_thread, 'threads': []}
        for frames in result['stacks']:
            symbolicated['threads'].append({'frames': frames})
        formatted_symbolications.append(symbolicated)
    return formatted_symbolications