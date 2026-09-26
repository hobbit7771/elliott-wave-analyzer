import json,sys,statistics as st,math
g=json.load(open(sys.argv[1])); minN=int(sys.argv[2])
ok=[r for r in g if r['seg']['TRAIN']['n']>=minN]
print('configs',len(g),'TRAIN n>=',minN,':',len(ok))
xs=[r['seg']['TRAIN']['avgR'] for r in ok]; ys=[r['seg']['VALIDATION']['avgR'] for r in ok]
mx,my=st.mean(xs),st.mean(ys); c=sum((a-mx)*(b-my) for a,b in zip(xs,ys))/math.sqrt(sum((a-mx)**2 for a in xs)*sum((b-my)**2 for b in ys)); print('corr TRAIN~VAL',round(c,3),'mean T',round(mx,3),'mean V',round(my,3))
print('share of configs with TRAIN>0:',round(sum(x>0 for x in xs)/len(xs),2),' VAL>0:',round(sum(y>0 for y in ys)/len(ys),2))
for k in ok[0]['g']:
  d={}
  for r in ok: d.setdefault(str(r['g'][k]),[]).append((r['seg']['TRAIN']['avgR'],r['seg']['VALIDATION']['avgR'],r['seg']['TRAIN']['n']))
  print(f"{k:16s}",{v:(round(st.mean(x[0] for x in a),3),round(st.mean(x[1] for x in a),3),round(st.mean(x[2] for x in a))) for v,a in sorted(d.items())})
ok.sort(key=lambda r:-r['seg']['TRAIN']['avgR'])
print('TOP 12 by TRAIN (T n avgR | V n avgR):')
for r in ok[:12]:
  s=r['seg']; print(f"T {s['TRAIN']['n']:4d} {s['TRAIN']['avgR']:+.3f} | V {s['VALIDATION']['n']:4d} {s['VALIDATION']['avgR']:+.3f} | {json.dumps({k:v for k,v in r['g'].items() if k!='levelSource'})}")
