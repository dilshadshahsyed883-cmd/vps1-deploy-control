"""CPU-only real CLIP vision/text inference. No filename tags or downloaded code.

MIT-licensed OpenAI CLIP, ONNX conversion from Xenova. Pinned artifacts, one
cached copy. Scores are candidate-relative model suggestions, not calibrated
probabilities. Motion, identity and speech cannot be inferred by still CLIP.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

MODEL = 'Xenova/clip-vit-base-patch32'
REVISION = 'd15189d7028b43f1d3e65039190477f6af591c2a'
VERSION = MODEL + '@' + REVISION + ':int8:v1'
ARTIFACTS = {item["path"]:item["sha256"] for item in [
    {"path":'onnx/vision_model_quantized.onnx', "sha256":'583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299'},
    {"path":'onnx/text_model_quantized.onnx', "sha256":'73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a'},
    {"path":'tokenizer.json', "sha256":'f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd'},
]}
CATEGORIES = {
 'objects': ['person','group of people','car','truck','train','airplane','boat','bicycle','dog','cat','tiger','bird','horse','elephant','flower','tree','mountain','building','food','computer','furniture','water'],
 'environment': ['beach','ocean','forest','mountains and lake','city street','highway','office','home interior','desert','snowy landscape','farm','airport','studio','garden','kitchen','open sky'],
 'actions': ['walking','running','driving','flying','talking','working at a computer','cooking','swimming','resting','standing still'],
 'shot': ['close-up shot','medium shot','wide landscape shot','aerial view','portrait photograph','macro photograph'],
 'lighting': ['daylight','night','indoor lighting','studio lighting','golden hour sunset','soft overcast light','high contrast light'],
 'mood': ['peaceful atmosphere','dramatic atmosphere','energetic atmosphere','mysterious atmosphere'],
 'relationships': ['person inside a car','person near a building','animal in a forest','boat on water','flowers in a garden','no clear relationship'],
 'branding': ['visible watermark or logo overlay','photograph without a visible logo overlay'],
}


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def model_ready(folder):
    folder=Path(folder)
    try:
        manifest=json.loads((folder/'ready.json').read_text())
        return manifest.get('version')==VERSION and all((folder/p).is_file() and
            (folder/p).stat().st_size==manifest['sizes'][p] for p in ARTIFACTS)
    except (OSError,ValueError,KeyError):return False


def prepare_model(folder):
    import fcntl
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.download.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if model_ready(folder) and all(not expected or digest(folder/name)==expected for name,expected in ARTIFACTS.items()):return
        if shutil.disk_usage(folder).free<600*1024**2:raise RuntimeError('At least 600 MB free disk needed for AI model setup')
        sizes={}
        for name,expected in ARTIFACTS.items():
            target=folder/name;target.parent.mkdir(parents=True,exist_ok=True)
            if not target.is_file() or expected and digest(target)!=expected:
                tmp=target.with_suffix('.partial')
                try:
                    url=f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}'
                    with urllib.request.urlopen(url,timeout=90) as src,tmp.open('wb') as dst:
                        total=0
                        while chunk:=src.read(1024*1024):
                            total+=len(chunk)
                            if total>100*1024**2:raise RuntimeError('Unexpected model artifact size')
                            dst.write(chunk)
                    if expected and digest(tmp)!=expected:raise RuntimeError('Model integrity check failed')
                    tmp.replace(target)
                finally:tmp.unlink(missing_ok=True)
            sizes[name]=target.stat().st_size
        (folder/'ready.json').write_text(json.dumps({'version':VERSION,'sizes':sizes}))


class ClipEngine:
    def __init__(self,folder):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        if not model_ready(folder):raise RuntimeError('Local visual model not installed')
        self.np=np;folder=Path(folder)
        from ai_text import SpeechEngine
        self.speech=SpeechEngine(folder.parent/"speech_models")
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        options.log_severity_level=3
        self.vision=ort.InferenceSession(str(folder/'onnx/vision_model_quantized.onnx'),options,providers=['CPUExecutionProvider'])
        self.text=ort.InferenceSession(str(folder/'onnx/text_model_quantized.onnx'),options,providers=['CPUExecutionProvider'])
        self.tokenizer=Tokenizer.from_file(str(folder/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=77)
        self.tokenizer.enable_padding(length=77,pad_id=49407,pad_token='<|endoftext|>')
        self.labels={k:self.embed_text(['a photo of '+x for x in v]) for k,v in CATEGORIES.items()}

    def normalize(self,x):
        np=self.np;return x/np.maximum(np.linalg.norm(x,axis=-1,keepdims=True),1e-12)

    def embed_text(self,texts):
        np=self.np
        tokens=self.tokenizer.encode_batch(texts)
        supplied={'input_ids':np.asarray([x.ids for x in tokens],dtype=np.int64),
                  'attention_mask':np.asarray([x.attention_mask for x in tokens],dtype=np.int64)}
        feeds={i.name:supplied[i.name] for i in self.text.get_inputs()}
        outputs=self.text.run(None,feeds)
        names=[x.name for x in self.text.get_outputs()]
        idx=next((i for i,n in enumerate(names) if 'text_embeds' in n),0)
        return self.normalize(outputs[idx])

    def embed_image(self,image):
        from PIL import ImageOps
        np=self.np
        image=ImageOps.exif_transpose(image).convert('RGB')
        w,h=image.size;scale=224/min(w,h)
        image=image.resize((round(w*scale),round(h*scale)),resample=3)
        w,h=image.size;image=image.crop(((w-224)//2,(h-224)//2,(w+224)//2,(h+224)//2))
        pixels=np.asarray(image,dtype=np.float32)/255.
        pixels=(pixels-np.array([.48145466,.4578275,.40821073],dtype=np.float32))/np.array([.26862954,.26130258,.27577711],dtype=np.float32)
        outputs=self.vision.run(None,{'pixel_values':pixels.transpose(2,0,1)[None].astype(np.float32)})
        names=[x.name for x in self.vision.get_outputs()]
        idx=next((i for i,n in enumerate(names) if 'image_embeds' in n),0)
        return self.normalize(outputs[idx])[0]

    def analyze(self,path,media_type,duration=0,contact_path=None,audio_path=None,audio_start=0):
        from PIL import Image,ImageOps
        np=self.np;started=time.monotonic();frames=[];vectors=[];contact_images=[];ocr_results=[];crops=[]
        with tempfile.TemporaryDirectory(prefix='sml-vision-') as tmp:
            times=[0.] if media_type=='image' else sorted(set(round(max(0,duration)*f,3) for f in (.15,.5,.85)))
            for i,at in enumerate(times):
                frame=Path(path)
                if media_type!='image':
                    frame=Path(tmp)/f'{i}.png'
                    subprocess.run(['ffmpeg','-v','error','-threads','1','-ss',str(at),'-i',str(path),'-frames:v','1','-vf','scale=480:-2','-threads','1',str(frame)],check=True,capture_output=True,timeout=30)
                with Image.open(frame) as im:
                    if im.width*im.height>40000000:raise RuntimeError('Image dimensions exceed analysis memory budget')
                    im.load();im=ImageOps.exif_transpose(im).convert('RGB')
                    if contact_path:contact_images.append(ImageOps.pad(im,(240,160)))
                    vec=self.embed_image(im);vectors.append(vec)
                    measures=quality_metrics(im,np)
                    from ai_text import recognize_text
                    ocr_results.append(recognize_text(im,at))
                    fields={}
                    for group,labels in CATEGORIES.items():
                        similarity=self.labels[group]@vec
                        probability=np.exp(30*(similarity-similarity.max()));probability/=probability.sum()
                        best=np.argsort(similarity)[::-1][:3 if group=='objects' else 1]
                        fields[group]=[{'value':labels[int(j)],'confidence':round(float(probability[j]),4),
                            'similarity':round(float(similarity[j]),4),'origin':'generated','provider':MODEL,
                            'model_version':VERSION,'timestamp':at,'uncertain':True} for j in best]
                    if i==len(times)//2:crops=self.crop_recommendations(im,fields['objects'][0]['value'])
                    frames.append({'timestamp':at,'fields':fields,'quality':measures})
        if contact_path and contact_images:
            strip=Image.new('RGB',(240*len(contact_images),160))
            for i,image in enumerate(contact_images):strip.paste(image,(i*240,0))
            Path(contact_path).parent.mkdir(parents=True,exist_ok=True)
            strip.save(contact_path,quality=85)
        vector=self.normalize(np.mean(vectors,axis=0)[None])[0]
        fields=frames[len(frames)//2]['fields']
        description='Visual model suggestions: '+', '.join(fields[k][0]['value'] for k in ('objects','environment','shot','lighting'))+'. Review uncertain tags.'
        return {'model':MODEL,'model_version':VERSION,'description':description,'fields':fields,
                'crop_recommendations':crops,'review_alerts':['Possible logo/watermark: review source rights'] if fields['branding'][0]['value']=='visible watermark or logo overlay' else [],
                'frames':frames,'elapsed_seconds':round(time.monotonic()-started,3),
                'confidence_note':'Relative scores within a fixed candidate set; not calibrated probabilities. Actions, lighting and mood are suggestions. Still frames cannot verify camera movement.',
                'unsupported':['camera_movement','identity'],
                'ocr':{'state':'complete' if any(x['state']=='complete' for x in ocr_results) else 'not_configured','text':' '.join(dict.fromkeys(x['text'] for x in ocr_results if x['text'])),'frames':ocr_results},
                'transcript':self.speech.transcribe(audio_path or path,duration,audio_start) if media_type!='image' else {'state':'not_applicable','text':''},
                'quality':frames[len(frames)//2]['quality'],
                'embedding':vector.tolist()}


    def crop_recommendations(self,image,subject):
        w,h=image.size;target=self.labels['objects'][CATEGORIES['objects'].index(subject)];out=[]
        for name,ratio in [('16:9',16/9),('9:16',9/16),('1:1',1.)]:
            cw=min(w,int(h*ratio));ch=min(h,int(w/ratio));cw=max(1,cw);ch=max(1,ch)
            candidates=[]
            for fraction in (0.,.5,1.):
                x=round((w-cw)*fraction);y=round((h-ch)*fraction)
                score=float(self.embed_image(image.crop((x,y,x+cw,y+ch)))@target)
                candidates.append((score,x,y))
            score,x,y=max(candidates)
            out.append({'aspect':name,'x':x,'y':y,'width':cw,'height':ch,'subject_suggestion':subject,
                        'relevance':round(score,4),'uncertain':True,'method':'CLIP-ranked feasible crop windows',
                        'note':'Review framing; this is not an object bounding box and does not alter the original.'})
        return out


def quality_metrics(image,np):
    from PIL import Image
    small=image.resize((128,128)).convert('L');a=np.asarray(small,dtype=np.float32)
    lap=-4*a[1:-1,1:-1]+a[:-2,1:-1]+a[2:,1:-1]+a[1:-1,:-2]+a[1:-1,2:]
    sharp=float(lap.var());blank=float(((a<16)|(a>240)).mean())
    detail=min(1,sharp/500);score=round(100*(.6*detail+.4*(1-blank)))
    ph=image.resize((9,8)).convert('L');p=np.asarray(ph)
    bits=(p[:,1:]>p[:,:-1]).reshape(-1);hashvalue=sum(int(b)<<i for i,b in enumerate(bits))
    palette=image.resize((64,64)).quantize(colors=4).convert('RGB').getcolors(4096) or []
    colors=['#%02x%02x%02x'%color for n,color in sorted(palette,reverse=True)[:4]]
    rgb=np.asarray(image.resize((64,64)),dtype=np.float32).mean(axis=(0,1))
    tone='warm' if rgb[0]-rgb[2]>10 else 'cool' if rgb[2]-rgb[0]>10 else 'neutral'
    return {'score':score,'color_tone':tone,'laplacian_variance':round(sharp,2),'near_blank_fraction':round(blank,4),
            'dhash':f'{hashvalue:016x}','dominant_colors':colors,
            'advisory':'Sharpness and blank fractions are review signals; no automatic deletion or identity inference.'}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--prepare',type=Path,required=True);a=p.parse_args()
    prepare_model(a.prepare)
    print(json.dumps({'ready':model_ready(a.prepare),'model_version':VERSION}))
