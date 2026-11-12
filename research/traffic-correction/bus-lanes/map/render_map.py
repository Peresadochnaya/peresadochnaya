"""Render exact offline coordinates to standalone SVG, PNG and HTML."""
from pathlib import Path
import json,os,html,hashlib
os.environ['MPLCONFIGDIR']='/tmp/mpl-bus-map'
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
base=Path(__file__).resolve().parent;repo=base.parents[3]
lanes=json.loads((repo/'backend/app/fixtures/traffic/osm-bus-lanes.geojson').read_text())['features'];routes=json.loads((base/'route-map-data.json').read_text());roads=json.loads((base/'background-roads.json').read_text());official=json.loads((repo/'backend/app/fixtures/traffic/official-resolved-lanes.geojson').read_text())['features'];colors=['#6941c6','#d25a16','#1677bc']
def xy(line):return [((x-30.3)*55.8,(y-59.9)*111.2) for x,y in line]
def draw(ax,lines,color,width=1,alpha=1,style='solid',zorder=1):
 arr=[xy(c) for c in lines if len(c)>1]
 if arr:ax.add_collection(LineCollection(arr,colors=color,linewidths=width,alpha=alpha,linestyles=style,zorder=zorder))
def frame(ax,bounds):
 cx=(bounds[0]+bounds[1])/2;cy=(bounds[2]+bounds[3])/2;w=bounds[1]-bounds[0];h=bounds[3]-bounds[2];w=max(w,h*1.25);h=max(h,w/1.25);ax.set_xlim(cx-w/2,cx+w/2);ax.set_ylim(cy-h/2,cy+h/2);ax.set_aspect('equal');ax.set_facecolor('#fafbf9');ax.grid(color='#e4e8e3',linewidth=.45);ax.tick_params(labelsize=8);ax.set_xlabel('км к востоку от 30.3°E',fontsize=8);ax.set_ylabel('км к северу от 59.9°N',fontsize=8)
def bounds(lines,pad=0.5):
 points=[p for line in lines for p in xy(line)];xs=[p[0] for p in points];ys=[p[1] for p in points];return min(xs)-pad,max(xs)+pad,min(ys)-pad,max(ys)+pad
fig,axes=plt.subplots(2,2,figsize=(16,13));axes=axes.flatten();allroute=[l['geometry'] for r in routes for l in r.get('legs',[])];b=bounds(allroute,2)
draw(axes[0],roads,'#d8ded8',.5);draw(axes[0],[f['geometry']['coordinates'] for f in lanes],'#20a065',1.6)
for i,r in enumerate(routes):
 for leg in r.get('legs',[]):
  if leg['mode'] in ('bus','trolleybus'):draw(axes[0],[leg['geometry']],colors[i],2.3,zorder=3)
draw(axes[0],[f['geometry']['coordinates'] for f in official],'#bc2095',3,zorder=4)
frame(axes[0],b);axes[0].set_title('Слой полос OSM\nТри сохранённых автобусных поиска',loc='left',fontweight='bold',fontsize=12)
axes[0].legend(handles=[Line2D([0],[0],color='#20a065',lw=3,label='OSM: направленные полосы'),Line2D([0],[0],color='#bc2095',lw=3,label='Официально: Малая Морская')]+[Line2D([0],[0],color=colors[i],lw=3,label=r.get('route_name',r['title'])) for i,r in enumerate(routes)],fontsize=8,loc='lower left')
for i,r in enumerate(routes):
 ax=axes[i+1];draw(ax,roads,'#dde1dd',.6);draw(ax,[f['geometry']['coordinates'] for f in lanes],'#6ac69b',2,alpha=.7)
 for leg in r.get('legs',[]):
  if leg['mode']=='wait':continue
  draw(ax,[leg['geometry']],colors[i] if leg['mode'] in ('bus','trolleybus') else '#86909e',3 if leg['mode'] in ('bus','trolleybus') else 1.3,style='solid' if leg['mode'] in ('bus','trolleybus') else 'dotted',zorder=3)
 draw(ax,r.get('protected',[]),'#082f20',6,zorder=6)
 draw(ax,r.get('unknown',[]),'#ef324b',2.2,style='dashed',zorder=5)
 for label,point in [('A',r['legs'][0]['geometry'][0]),('B',r['legs'][-1]['geometry'][-1])]:
  x,y=xy([point])[0];ax.scatter([x],[y],s=55,c='white',edgecolors=colors[i],zorder=7);ax.annotate(label,(x,y),xytext=(6,4),textcoords='offset points',fontsize=10,fontweight='bold',zorder=8)
 bb=bounds([l['geometry'] for l in r['legs']],.35);frame(ax,bb)
 ax.set_title(r['title'],fontsize=11,loc='left',fontweight='bold')
 debug=r['debug'];ratio=debug['coverage'].get('usable_coverage_ratio');coverage='—' if ratio is None else f'{ratio:.1%}'
 ax.text(.02,.02,f"Полоса на маршруте: {r['protected_m']:.1f} м\nТрафик: {coverage}; задержка +{debug['added_delay_seconds']} с",transform=ax.transAxes,fontsize=9,bbox=dict(facecolor='white',edgecolor='#ddd',alpha=.95),zorder=10)
 if r['protected_m']>0:
  p=xy(r['protected'][0])[0];ax.annotate(f"защищено {r['protected_m']:.1f} м",p,xytext=(30,30),textcoords='offset points',arrowprops=dict(arrowstyle='->',color='#082f20'),fontsize=9,color='#082f20',zorder=11)
 ax.legend(handles=[Line2D([0],[0],color=colors[i],lw=3,label='Фактический автобусный вариант'),Line2D([0],[0],color='#082f20',lw=5,label='Точный защищённый интервал'),Line2D([0],[0],color='#ef324b',lw=2,ls='--',label='Нет пригодного трафика / полоса неизвестна')],loc='upper right',fontsize=7)
fig.suptitle('Выделенные полосы: данные отдельно, применение на маршруте отдельно',fontsize=18,fontweight='bold',x=.04,ha='left')
fig.text(.04,.945,'12 ноября 2026 · финальный слой: 433 OSM + 6 официальных частей · три live-поиска: исходный слой 672/1695',fontsize=10,color='#4a5750')
fig.text(.04,.025,'Зелёный слой — явные OSM-теги; он не доказывает полное покрытие официальных коридоров. Красный пунктир не означает отсутствие полосы.\nДаты OSM — даты правки объекта, не наблюдения знаков. Подложка: дороги локального PBF; без тайлов и внешних запросов. © OpenStreetMap contributors (ODbL).',fontsize=9,color='#4a5750')
fig.subplots_adjust(top=.91,bottom=.10,hspace=.25,wspace=.15)
fig.savefig(base/'coverage-and-three-routes.png',dpi=170);fig.savefig(base/'coverage-and-three-routes.svg')
svg=(base/'coverage-and-three-routes.svg').read_text();svg=svg[svg.index('<svg'):]
rows=''.join(f"<tr><td>{html.escape(r['title'])}</td><td>{r['protected_m']:.2f} м</td><td>+{r['debug']['added_delay_seconds']} с</td><td>{html.escape(r['debug']['evaluation_group'])}</td></tr>" for r in routes)
page=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Полосы OSM и три маршрута</title><style>body{{font:16px system-ui;margin:25px auto;max-width:1500px;padding:0 20px;color:#20352c}}svg{{width:100%;height:auto}}table{{border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}p{{max-width:1000px;line-height:1.5}}</style><h1>Слой полос и три реальных автобусных поиска</h1><p>Сохранённые ответы от 12 ноября 2026. Карта полностью автономна: открытие не делает сетевых запросов. Для каждого поиска показан первый возвращённый вариант с автобусом; пешеходные альтернативы не подменяют автобус.</p>{svg}<table><tr><th>Поиск</th><th>Защищённая длина</th><th>Добавленная задержка</th><th>Статус коррекции</th></tr>{rows}</table><p><b>34 из 37</b> записей источников имеют независимые свидетельства OSM на той же улице. Один официальный коридор — Малая Морская — разрешён по точным границам и показан пурпурным. Финальный слой содержит 674 пары GTFS / 1699 вариантов; три live-ответа получены с исходным слоем 672/1695.</p><p>Зелёный слой показывает направленные представления OSM way, а не 433 независимых физических полосы. Красные интервалы означают отсутствие пригодного покрытия трафиком и подтверждённой полосы, а не доказанное отсутствие выделенки. OSM timestamp — дата правки объекта. Направления сохранены в исходных GeoJSON и JSON.</p><p>Источники: локальный spb-lo.osm.pbf / OpenStreetMap contributors (ODbL), bundled road-status.json, три response-corrected-full.json. Точные интервалы восстановлены через TrafficGraph.trip_segments и slice_line. Ни одного API-запроса при построении карты.</p></html>'''
(base/'index.html').write_text(page)
print('Rendered',len(routes),'routes')
