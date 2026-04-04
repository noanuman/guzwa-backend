import math
import datetime

def distance(p1, p2):
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

def closest_point_on_segment(A, B, P):
    ax, ay = A
    bx, by = B
    px, py = P

    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay

    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq == 0:
        return A

    t = (apx * abx + apy * aby) / ab_len_sq
    t = max(0, min(1, t))

    return [ax + t * abx, ay + t * aby]


def closest_point_on_path(path_points, P):
    best_point = None
    best_distance = float('inf')
    best_index = -1

    for i in range(len(path_points) - 1):
        A = path_points[i]
        B = path_points[i + 1]

        candidate = closest_point_on_segment(A, B, P)
        d = distance(candidate, P)

        if d < best_distance:
            best_distance = d
            best_point = candidate
            best_index = i

    return best_point, best_distance, best_index

def add_times_simple(t1, t2):
    h1, m1 = map(int, t1.split(":"))
    h2, m2 = map(int, t2.split(":"))

    total_minutes = h1 * 60 + m1 + h2 * 60 + m2

    hours = (total_minutes // 60) % 24
    minutes = total_minutes % 60

    return f"{hours:02d}:{minutes:02d}"
