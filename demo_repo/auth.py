def login(payload):
    email = payload["email"]
    return {"status": 200, "email": email.lower()}
