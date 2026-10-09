from astropy.table import Table

from astro_pipeline.membership import _coordinate_epoch


def test_j2000_equinox_does_not_imply_observation_epoch():
    table = Table({"RAJ2000": [240.0], "DEJ2000": [-20.0]})
    table["RAJ2000"].description = "Right ascension (J2000)"
    table["DEJ2000"].description = "Declination (J2000)"
    kwargs = {"ra_column": "RAJ2000", "dec_column": "DEJ2000", "family": "raj2000"}
    assert _coordinate_epoch(table, **kwargs) == (None, "not_declared")
    table["RAJ2000"].description = "Right ascension (J2000), reference epoch J2015.5"
    assert _coordinate_epoch(table, **kwargs) == ("J2015.5", "column_descriptions")
