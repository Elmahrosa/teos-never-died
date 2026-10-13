import pytest
from dataclasses import dataclass
from api_envelope import parse_envelope, APIError


@dataclass
class Point:
    x: int
    y: int


@dataclass
class EmptyData:
    pass


def test_valid_envelope():
    raw = '{"status":"success","data":{"x":1,"y":2}}'
    result = parse_envelope(raw, Point)
    assert result == Point(1, 2)


def test_unwrapped_payload():
    raw = '{"x":1,"y":2}'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "malformed"


def test_status_not_success():
    raw = '{"status":"forbidden","message":"quota"}'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "forbidden"
    assert exc_info.value.message == "quota"


def test_truncated_json():
    raw = '{"status":"success","data":{"x":1'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "malformed"


def test_invalid_data_missing_field():
    @dataclass
    class RequiredField:
        x: int
        y: int  # required

    raw = '{"status":"success","data":{"x":1}}'  # missing y
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, RequiredField)
    assert exc_info.value.status == "invalid_data"
    # Check that the message mentions the missing field (y)
    assert "y" in exc_info.value.message.lower()


def test_bytes_accepted():
    raw = b'{"status":"success","data":{}}'
    result = parse_envelope(raw, EmptyData)
    assert isinstance(result, EmptyData)


# Additional edge cases
def test_missing_status_field():
    raw = '{"data":{}}'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "malformed"
    assert "Missing 'status' field" in exc_info.value.message


def test_top_level_not_dict():
    # Top-level is a JSON string (not a dict)
    raw = '"hello"'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "malformed"
    assert "Top-level is not a dict" in exc_info.value.message


def test_top_level_not_dict_number():
    # Top-level is a JSON number
    raw = "42"
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "malformed"
    assert "Top-level is not a dict" in exc_info.value.message


def test_non_success_status_without_message():
    raw = '{"status":"error"}'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Point)
    assert exc_info.value.status == "error"
    assert exc_info.value.message == "server returned status=error"


def test_data_is_not_dict_but_model_is_dataclass_wrong_arg_count():
    @dataclass
    class Simple:
        x: int
        y: int

    # Pass a list with insufficient elements for the dataclass constructor
    raw = '{"status":"success","data":[1]}'
    with pytest.raises(APIError) as exc_info:
        parse_envelope(raw, Simple)
    assert exc_info.value.status == "invalid_data"