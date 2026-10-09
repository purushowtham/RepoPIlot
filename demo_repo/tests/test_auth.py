from auth import login

def test_valid_email():
    assert login({"email": "HELLO@example.com"})["email"] == "hello@example.com"
