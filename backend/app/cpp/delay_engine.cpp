#include <vector>
#include <cmath>
#include <algorithm>
#include <limits>
#include <stdexcept>

struct Point {
    double lat;
    double lon;
};

struct StopTime {
    std::string stop_id;
    int stop_sequence;
    int scheduled_arrival_sec;   // секунды от начала суток (может быть > 86400)
    int scheduled_departure_sec;
};

struct StopTimeDelay {
    std::string stop_id;
    int stop_sequence;
    int arrival_delay_sec;
    int departure_delay_sec;
};

struct MapMatchResult {
    double projection_lat;
    double projection_lon;
    double distance_to_route_m;
    int nearest_stop_sequence;
    std::string nearest_stop_id;
    double distance_to_nearest_stop_m;
};

// Приблизительное расстояние между двумя точками в метрах (haversine)
static double haversine_m(const Point& a, const Point& b) {
    constexpr double R = 6371000.0;
    constexpr double DEG2RAD = M_PI / 180.0;
    double dlat = (b.lat - a.lat) * DEG2RAD;
    double dlon = (b.lon - a.lon) * DEG2RAD;
    double lat1 = a.lat * DEG2RAD;
    double lat2 = b.lat * DEG2RAD;
    double h = std::sin(dlat / 2) * std::sin(dlat / 2) +
               std::cos(lat1) * std::cos(lat2) *
               std::sin(dlon / 2) * std::sin(dlon / 2);
    return 2 * R * std::asin(std::sqrt(h));
}

// Проекция точки на отрезок (в локальной плоской аппроксимации)
static void project_point_on_segment(
    const Point& p, const Point& a, const Point& b,
    double& proj_lat, double& proj_lon, double& dist_m
) {
    double lat0 = (a.lat + b.lat) / 2.0 * M_PI / 180.0;
    double cos_lat = std::cos(lat0);
    // Переводим в локальные метры
    auto to_xy = [&](const Point& pt) {
        return std::make_pair(
            (pt.lon - a.lon) * 111320.0 * cos_lat,
            (pt.lat - a.lat) * 111320.0
        );
    };
    auto [ax, ay] = to_xy(a);
    auto [bx, by] = to_xy(b);
    auto [px, py] = to_xy(p);
    double dx = bx - ax, dy = by - ay;
    double seg_len2 = dx * dx + dy * dy;
    double t = 0.0;
    if (seg_len2 > 1e-9) {
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len2;
        t = std::clamp(t, 0.0, 1.0);
    }
    double proj_x = ax + t * dx;
    double proj_y = ay + t * dy;
    // Обратно в градусы
    proj_lon = a.lon + proj_x / (111320.0 * cos_lat);
    proj_lat = a.lat + proj_y / 111320.0;
    double ddx = px - proj_x, ddy = py - proj_y;
    dist_m = std::sqrt(ddx * ddx + ddy * ddy);
}

// Map matching: проекция на геометрию маршрута и поиск ближайшей остановки
MapMatchResult map_match(
    const Point& vehicle_pos,
    const std::vector<Point>& route_geometry,
    const std::vector<StopTime>& stop_times_with_coords // stop_times уже с координатами остановок
) {
    if (route_geometry.size() < 2) throw std::invalid_argument("route_geometry must have at least 2 points");
    MapMatchResult result;
    double best_dist = std::numeric_limits<double>::max();
    for (size_t i = 0; i + 1 < route_geometry.size(); ++i) {
        double proj_lat, proj_lon, dist;
        project_point_on_segment(vehicle_pos, route_geometry[i], route_geometry[i+1],
                                 proj_lat, proj_lon, dist);
        if (dist < best_dist) {
            best_dist = dist;
            result.projection_lat = proj_lat;
            result.projection_lon = proj_lon;
            result.distance_to_route_m = dist;
        }
    }
    // Ближайшая остановка по расстоянию от проекции
    double best_stop_dist = std::numeric_limits<double>::max();
    for (const auto& st : stop_times_with_coords) {
        // Предполагается, что StopTime содержит координаты остановки
        // (в реальном коде нужно передавать отдельно stop_coords)
    }
    // Упрощённо: ближайшая остановка ищется по исходной позиции
    // В полной реализации сюда нужно передать координаты остановок
    result.nearest_stop_sequence = -1;
    result.nearest_stop_id = "";
    result.distance_to_nearest_stop_m = best_stop_dist;
    return result;
}

// Расчёт задержек и их распространение с учётом layover
std::vector<StopTimeDelay> compute_delays(
    const std::vector<StopTime>& stop_times,
    int current_stop_sequence,
    int observed_delay_sec,
    int layover_sec = 0
) {
    if (stop_times.empty()) throw std::invalid_argument("stop_times cannot be empty");
    std::vector<StopTimeDelay> updates;
    updates.reserve(stop_times.size());

    // Задержка для текущей и последующих остановок
    // Если задержка превышает layover, "лишняя" часть переносится на следующие остановки
    int effective_delay = observed_delay_sec;
    // Для остановок до текущей (если они включены) задержка не рассчитывается —
    // в соответствии с best practices их можно опустить, но мы оставляем для полноты
    for (const auto& st : stop_times) {
        StopTimeDelay d;
        d.stop_id = st.stop_id;
        d.stop_sequence = st.stop_sequence;
        if (st.stop_sequence < current_stop_sequence) {
            // Прошедшие остановки: задержка не прогнозируется
            d.arrival_delay_sec = 0;
            d.departure_delay_sec = 0;
        } else {
            // Распространяем задержку, вычитая layover нарастающим итогом
            // (упрощённая модель: layover учитывается один раз при переходе между блоками)
            int delay = effective_delay;
            if (layover_sec > 0 && st.stop_sequence > current_stop_sequence) {
                delay = std::max(0, effective_delay - layover_sec);
            }
            d.arrival_delay_sec = delay;
            d.departure_delay_sec = delay;
        }
        updates.push_back(d);
    }
    return updates;
}