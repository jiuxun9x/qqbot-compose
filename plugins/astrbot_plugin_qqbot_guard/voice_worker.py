"""Bounded audio decoding only; remote models perform speech recognition."""
from array import array
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import wave


def prepare(source, target, max_seconds=60):
    source=Path(source);target=Path(target)
    if not source.is_file() or source.stat().st_size>4*1024**2:raise ValueError('voice_size_limit')
    head=source.read_bytes()[:32]
    is_silk=head.startswith((b'#!SILK_V3',b'\x02#!SILK_V3'))
    supported=(is_silk or head.startswith((b'RIFF',b'ID3',b'OggS',b'fLaC',b'#!AMR'))
               or head[:1]==b'\xff' or head[4:8]==b'ftyp')
    if not supported:raise ValueError('voice_decode_failed')
    if is_silk:
        import pilk
        pcm=target.with_suffix('.pcm')
        pilk.decode(str(source),str(pcm),pcm_rate=24000)
        if pcm.stat().st_size>24000*2*(max_seconds+1):raise ValueError('voice_duration_limit')
        intermediate=target.with_name('silk-decoded.wav')
        with wave.open(str(intermediate),'wb') as audio:
            audio.setparams((1,2,24000,0,'NONE','NONE'));audio.writeframes(pcm.read_bytes())
        source=intermediate
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-threads','1',
        '-protocol_whitelist','file,pipe','-i',str(source),'-map','0:a:0','-vn','-ac','1','-ar','16000',
        '-t',str(max_seconds+1),'-c:a','pcm_s16le','-f','wav','-y',str(target)],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
    target.chmod(0o600)
    with wave.open(str(target),'rb') as audio:
        assert audio.getsampwidth()==2 and audio.getnchannels()==1 and audio.getframerate()==16000
        duration=audio.getnframes()/16000
        if duration>max_seconds:raise ValueError('voice_duration_limit')
        if duration<0.2:raise ValueError('voice_silent')
        samples=array('h',audio.readframes(audio.getnframes()))
    if sys.byteorder!='little':samples.byteswap()
    rms=math.sqrt(sum(value*value for value in samples)/max(1,len(samples)))
    peak=max((abs(value) for value in samples),default=0)
    # Fail closed on digital silence / near-zero recordings before any API call.
    if rms<35 and peak<200:raise ValueError('voice_silent')
    return {'duration_seconds':round(duration,3),'sample_rate':16000,'bytes':target.stat().st_size}


if __name__=='__main__':
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS,(384*1024**2,384*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(10,10))
    resource.setrlimit(resource.RLIMIT_FSIZE,(4*1024**2,4*1024**2))
    try:
        result=prepare(sys.argv[1],sys.argv[2],int(sys.argv[3]))
        print(json.dumps({'ok':True,'audio':result}))
    except Exception as error:
        allowed={'voice_size_limit','voice_duration_limit','voice_silent','voice_decode_failed'}
        code=str(error) if isinstance(error,ValueError) and str(error) in allowed else 'voice_decode_failed'
        print(json.dumps({'ok':False,'error':code}))
