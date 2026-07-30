from twod_fim_jobs.utils.hashing import hash_dict


def test_hash_dict():
    test_dict = {
        "B": "simple",
        "A": {"B": "nested", "A": 0, "Z": [1, 2, 3]},
        "X": ["X"],
        "internal spaces key": "internal spaces value",
    }
    assert hash_dict(test_dict, role_length=8) == "0699d27a"
    assert hash_dict(test_dict, role_length=16) == "0699d27a86a9ec62"
