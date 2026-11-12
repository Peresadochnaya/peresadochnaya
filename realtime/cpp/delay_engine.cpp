// MR24 delay propagation, repaired map matching and executable batch protocol.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <vector>
struct Point { double lat, lon; };
struct Stop { int sequence; std::int64_t arrival, departure; Point point; };
struct Match { double distance, progress; };
static Match match(Point p, const std::vector<Point>& shape) {
    Match best{std::numeric_limits<double>::infinity(), 0};
    double travelled=0;
    for (size_t i=1;i<shape.size();++i) {
        auto a=shape[i-1], b=shape[i];
        double scale=111320*std::cos((a.lat+b.lat)*std::acos(-1.0)/360);
        double dx=(b.lon-a.lon)*scale, dy=(b.lat-a.lat)*111320;
        double px=(p.lon-a.lon)*scale, py=(p.lat-a.lat)*111320;
        double length=std::hypot(dx,dy);
        double t=length>0 ? std::clamp((px*dx+py*dy)/(length*length),0.0,1.0):0;
        double d=std::hypot(px-t*dx,py-t*dy);
        if(d<best.distance) best={d, travelled+t*length};
        travelled+=length;
    }
    return best;
}
int main() {
    std::ios::sync_with_stdio(false);
    size_t count; if(!(std::cin>>count) || count>20000) return 2;
    for(size_t j=0;j<count;++j) {
        Point vehicle; std::int64_t timestamp; size_t n,m;
        if(!(std::cin>>vehicle.lat>>vehicle.lon>>timestamp>>n>>m) || n<2 || n>100000 || m<2 || m>10000) return 2;
        std::vector<Point> shape(n); for(auto& p:shape) if(!(std::cin>>p.lat>>p.lon)) return 2;
        std::vector<Stop> stops(m); for(auto& s:stops) if(!(std::cin>>s.sequence>>s.arrival>>s.departure>>s.point.lat>>s.point.lon)) return 2;
        auto pos=match(vehicle,shape);
        std::vector<double> progress; bool ordered=true;
        for(auto& s:stops) { double p=match(s.point,shape).progress; if(!progress.empty() && p<progress.back()-20) ordered=false; progress.push_back(p); }
        // Loops/ambiguous projections are deliberately not used to invent delays.
        if(!ordered || pos.distance>150) {std::cout<<"-1 0 0\n"; continue;}
        size_t next=0; while(next+1<m && progress[next]<pos.progress) ++next;
        double scheduled=static_cast<double>(stops[next].arrival);
        if(next>0 && progress[next]>progress[next-1]+1) {
            double ratio=std::clamp((pos.progress-progress[next-1])/(progress[next]-progress[next-1]),0.0,1.0);
            scheduled=stops[next-1].departure+ratio*(stops[next].arrival-stops[next-1].departure);
        }
        auto delay=static_cast<std::int64_t>(std::llround(timestamp-scheduled));
        if(std::abs(delay)>7200) {std::cout<<"-1 0 0\n"; continue;}
        std::cout<<stops[next].sequence<<' '<<delay<<' '<<std::setprecision(12)<<pos.distance<<'\n';
    }
}
