import stt_function_v3 as stt


def test_sanitize_filename_sanitizes_extension_and_preserves_normal_name():
    sanitized = stt.sanitize_filename("x 마세요. / [y]")

    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert stt.sanitize_filename("name.txt") == "name.txt"
