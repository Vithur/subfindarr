import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import pack

FAILED=[]
def check(n,c,d=''):
    print(('PASS ' if c else 'FAIL ')+n+((' :: '+d) if d and not c else ''))
    if not c: FAILED.append(n)

tmp=Path(tempfile.mkdtemp())
src=tmp/'in.srt'; src.write_text('1\n',encoding='utf-8')
v=Path('/media/movies/X (2020)/X.2020.mkv')

out=pack.install(src, v, tmp/'a', '.zh-cn.srt')
check('install 透传 tag', out.name=='X.2020.zh-cn.srt', out.name)

out2=pack.install(src, v, tmp/'b', '.chs.srt')
check('install 透传第二 tag', out2.name=='X.2020.chs.srt', out2.name)

out3=pack.install(src, v, tmp/'c')
check('install 默认改为 zh-cn', out3.name=='X.2020.zh-cn.srt', out3.name)

check('target_name 显式 tag', pack.target_name(v,'.zh-cn.srt').name=='X.2020.zh-cn.srt')
print()
print('全部通过' if not FAILED else f'{len(FAILED)} 项失败: {FAILED}')
sys.exit(1 if FAILED else 0)
