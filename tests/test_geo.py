from rental_finder.geo import distance_km


def test_distance_close():
    a = (-23.586, -46.672)
    b = (-23.590, -46.680)
    assert 0 < distance_km(a, b) < 1.5


def test_distance_symmetric():
    a = (-23.586, -46.672)
    b = (-23.610, -46.700)
    assert abs(distance_km(a, b) - distance_km(b, a)) < 1e-9
