import country_converter as coco

_cc = coco.CountryConverter()


def get_continent(alpha2: str) -> str:
    """Return the continent name for an ISO alpha-2 country code, or 'Unknown'."""
    result = _cc.convert(alpha2, src="ISO2", to="continent")
    if result == "not found":
        return "Unknown"
    if result == "America":
        region = _cc.convert(alpha2, src="ISO2", to="UNregion")
        if region == "South America":
            return "South America"
        return "North America"
    return result


def get_country_name(alpha2: str) -> str:
    """Return the short country name for an ISO alpha-2 code, or the code itself."""
    result = _cc.convert(alpha2, src="ISO2", to="name_short")
    return result if result != "not found" else alpha2
