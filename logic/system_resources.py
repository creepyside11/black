"""Read-only VPS metrics; no additional dependency or service changes."""
import os
import shutil
import time


def _cpu():
    with open('/proc/stat', encoding='ascii') as f:
        fields=f.readline().split()
    if fields[0] != 'cpu' or len(fields)<5: raise ValueError('CPU unavailable')
    numbers=[int(x) for x in fields[1:9]]
    return sum(numbers), numbers[3]+(numbers[4] if len(numbers)>4 else 0)


def snapshot(interval=.3):
    out={'cpu':None,'ram_total':None,'ram_available':None,'disk_total':None,'disk_free':None}
    try:
        a,ia=_cpu();time.sleep(interval);b,ib=_cpu()
        if b>a:out['cpu']=max(0.,min(100.,100*(1-(ib-ia)/(b-a))))
    except (OSError,ValueError,IndexError):pass
    try:
        with open('/proc/meminfo',encoding='ascii') as f:
            mem={parts[0][:-1]:int(parts[1])*1024 for line in f if (parts:=line.split()) and parts[0] in ('MemTotal:','MemAvailable:')}
        out['ram_total'],out['ram_available']=mem['MemTotal'],mem['MemAvailable']
    except (OSError,ValueError,KeyError,IndexError):pass
    try:
        disk=shutil.disk_usage(os.path.abspath(os.sep));out['disk_total'],out['disk_free']=disk.total,disk.free
    except OSError:pass
    return out


def format_snapshot(data):
    gib=1024**3
    cpu=data.get('cpu');total=data.get('ram_total');available=data.get('ram_available')
    dt=data.get('disk_total');free=data.get('disk_free')
    a=f'{cpu:.1f}%' if cpu is not None else 'недоступно'
    b=f'{(total-available)/gib:.2f} / {total/gib:.2f} ГиБ (доступно {available/gib:.2f})' if total is not None and available is not None else 'недоступно'
    c=f'{(dt-free)/gib:.2f} / {dt/gib:.2f} ГиБ (свободно {free/gib:.2f})' if dt is not None and free is not None else 'недоступно'
    return f'📊 Ресурсы сервера\nCPU: {a}\nRAM: {b}\nДиск /: {c}\nЗначения системы; лимиты контейнера могут отличаться.'
