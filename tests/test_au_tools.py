import pytest

from au_opendata_mcp.services.au_tools import au_financial_year, validate_abn

# --- validate_abn ---


@pytest.mark.parametrize(
    "abn",
    [
        "51824753556",  # the ATO's published example
        "51 824 753 556",  # the same, in its conventional spacing
        "51-824-753-556",  # hyphenated
        " 51824753556 ",  # padded
    ],
)
def test_a_valid_abn_is_accepted_in_any_conventional_formatting(abn):
    result = validate_abn(abn)
    assert result.valid is True
    assert result.normalised == "51 824 753 556"
    assert result.reason is None


def test_a_single_digit_mutation_fails_the_checksum():
    result = validate_abn("51824753557")
    assert result.valid is False
    assert "checksum" in result.reason.lower()
    assert result.normalised is None


def test_a_random_eleven_digit_number_fails():
    assert validate_abn("12345678901").valid is False


@pytest.mark.parametrize(
    "abn,expected",
    [
        ("5182475355", "11 digits"),
        ("518247535567", "11 digits"),
        ("", "11 digits"),
    ],
)
def test_wrong_length_is_reported_as_a_length_problem(abn, expected):
    result = validate_abn(abn)
    assert result.valid is False
    assert expected in result.reason


def test_non_digit_characters_are_reported_distinctly():
    result = validate_abn("5182475355X")
    assert result.valid is False
    assert "digit" in result.reason.lower()


# --- au_financial_year ---


@pytest.mark.parametrize(
    "date,label,start,end",
    [
        ("2026-09-23", "FY2026–27", "2026-07-01", "2027-06-30"),
        ("2026-07-01", "FY2026–27", "2026-07-01", "2027-06-30"),  # first day of the FY
        ("2026-06-30", "FY2025–26", "2025-07-01", "2026-06-30"),  # last day of the prior FY
        ("2026-01-15", "FY2025–26", "2025-07-01", "2026-06-30"),
        ("2025-12-31", "FY2025–26", "2025-07-01", "2026-06-30"),
    ],
)
def test_the_financial_year_boundary_is_the_first_of_july(date, label, start, end):
    result = au_financial_year(date)
    assert result.label == label
    assert result.start_date == start
    assert result.end_date == end


def test_a_century_boundary_formats_the_end_year_correctly():
    assert au_financial_year("2099-08-01").label == "FY2099–00"


def test_an_unparseable_date_raises_value_error():
    with pytest.raises(ValueError, match="could not be read as a date"):
        au_financial_year("not a date")


def test_a_datetime_string_is_accepted():
    assert au_financial_year("2026-09-23T14:30:00").label == "FY2026–27"
