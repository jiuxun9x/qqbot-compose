"""Resource-bounded still/animation decoding, producing ordinary JPEG frames."""
import bisect
import json
import os
from pathlib import Path
import resource
import sys
import warnings

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS=20_000_000
warnings.simplefilter('error',Image.DecompressionBombWarning)


def normalize(paths, folder, max_frames=6):
    if not 1<=len(paths)<=2:
        raise ValueError('too_many_images')
    folder=Path(folder)
    items=[]
    frames=[]
    for number,source in enumerate(paths):
        path=Path(source)
        if not path.is_file() or path.stat().st_size>12*1024**2:
            raise ValueError('image_too_large')
        budget=max(1,(max_frames-len(frames))//(len(paths)-number))
        with Image.open(path) as image:
            if image.format not in ('PNG','JPEG','WEBP','GIF'):
                raise ValueError('unsupported_image')
            if image.width*image.height>20_000_000:
                raise ValueError('image_too_large')
            count=getattr(image,'n_frames',1)
            animated=count>1
            duration=0
            boundaries=[]
            approximate=False
            scanned=1
            if animated:
                for index in range(min(count,240)):
                    image.seek(index)
                    if image.width*image.height>20_000_000:
                        raise ValueError('image_too_large')
                    value=image.info.get('duration',100)
                    if not isinstance(value,(int,float)) or not 20<=value<=10000:
                        value=100;approximate=True
                    boundaries.append(duration)
                    duration+=int(value)
                    scanned=index+1
                    if duration>=60000:break
                positions=sorted({max(0,bisect.bisect_right(boundaries,(duration-1)*i/max(1,budget-1))-1)
                                  for i in range(budget)})
            else:
                positions=[0];boundaries=[0]
            samples=[]
            for index in positions:
                image.seek(index)
                frame=ImageOps.exif_transpose(image).convert('RGBA')
                frame.thumbnail((960,960) if animated else (2560,2560),Image.Resampling.LANCZOS)
                rgb=Image.new('RGB',frame.size,'white')
                rgb.paste(frame,mask=frame.getchannel('A'))
                target=folder/(str(number)+'-'+str(index)+'.jpg')
                rgb.save(target,'JPEG',quality=88 if animated else 92)
                target.chmod(0o600)
                frames.append(str(target))
                samples.append({'index':index,'at_ms':boundaries[index],'path':str(target)})
                rgb.close();frame.close()
            items.append({'format':image.format,'animated':animated,'total_frames':count,
                          'scanned_frames':scanned,'duration_ms':duration if animated else None,
                          'timing_approximate':approximate,'truncated':animated and scanned<count,
                          'samples':samples})
    if sum(Path(path).stat().st_size for path in frames)>12*1024**2:
        raise ValueError('image_too_large')
    return {'frames':frames,'items':items}


if __name__=='__main__':
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS,(384*1024**2,384*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(8,8))
    resource.setrlimit(resource.RLIMIT_FSIZE,(12*1024**2,12*1024**2))
    try:
        result=normalize(json.loads(sys.argv[1]),sys.argv[2])
        print(json.dumps({'ok':True,'media':result}))
    except Exception:
        # Never expose source paths or image metadata in error messages.
        print(json.dumps({'ok':False,'error':'image_decode_failed'}))
