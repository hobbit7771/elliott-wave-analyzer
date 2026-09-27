import json,sys,statistics as st,math
for s in sys.argv[1:]:
  g=json.load(open(f'rg_{s}.json'))
  ok=[r for r in g if r['seg']['TRAIN']['n']>=25]
  xs=[r['seg']['TRAIN']['avgR'] for r in ok]; ys=[r['seg']['VALIDATION']['avgR'] for r in ok]
  mx,my=st.mean(xs),st.mean(ys)
  den=math.sqrt(sum((a-mx)**2 for a in xs)*sum((b-my)**2 for b in ys)) or 1
  c=sum((a-mx)*(b-my) for a,b in zip(xs,ys))/den
  print(f"\n=== {s}: {len(ok)} combos; corr T~V {c:+.2f}; mean T {mx:+.3f} V {my:+.3f}; share T>0 {sum(x>0 for x in xs)/len(xs):.2f} V>0 {sum(y>0 for y in ys)/len(ys):.2f}")
  for k in ok[0]['g']:
    d={}
    for r in ok: d.setdefault(str(r['g'][k]),[]).append((r['seg']['TRAIN']['avgR'],r['seg']['VALIDATION']['avgR']))
    print(f"  {k:14s}",{v:(round(st.mean(x[0] for x in a),2),round(st.mean(x[1] for x in a),2)) for v,a in sorted(d.items())})
  best=max(ok,key=lambda r:r['seg']['TRAIN']['avgR'])
  b=best['seg']; print('  best TRAIN:',json.dumps(best['g']),f"T {b['TRAIN']['n']} {b['TRAIN']['avgR']:+.2f} | V {b['VALIDATION']['n']} {b['VALIDATION']['avgR']:+.2f}")
