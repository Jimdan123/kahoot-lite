"""Regression test for quiz-CRUD edit/delete: prefill + cross-user 403.

    RATELIMIT_ENABLED=0 SECRET_KEY=dev-secret PORT=5001 python run.py
    BASE=http://localhost:5001 python tests/test_quiz_crud.py
"""
import os
import re
import requests

BASE = os.environ.get("BASE", "http://localhost:5001")


def csrf(session, url):
    r = session.get(url)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)


def signup(session, username):
    session.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(session, f"{BASE}/auth/signup"),
        "username": username, "password": "password123", "submit": "Sign Up",
    }, allow_redirects=False)


def main():
    owner, other = requests.Session(), requests.Session()
    signup(owner, "crudhost")
    signup(other, "crudother")

    r = owner.post(f"{BASE}/quiz/new", data={
        "csrf_token": csrf(owner, f"{BASE}/quiz/new"),
        "name": "CRUD Test Set", "description": "", "submit": "Save",
    }, allow_redirects=False)
    set_id = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))

    owner.post(f"{BASE}/quiz/{set_id}/questions/new", data={
        "csrf_token": csrf(owner, f"{BASE}/quiz/{set_id}/questions/new"),
        "text": "2+2?", "option_a": "3", "option_b": "4", "option_c": "", "option_d": "",
        "correct_option": "B", "time_limit": "20", "submit": "Save Question",
    }, allow_redirects=False)

    detail_html = owner.get(f"{BASE}/quiz/{set_id}").text
    m = re.search(r"questions/(\d+)/edit", detail_html)
    assert m, "expected an Edit link on detail.html"
    question_id = int(m.group(1))

    edit_html = owner.get(f"{BASE}/quiz/{set_id}/questions/{question_id}/edit").text
    assert '2+2?' in edit_html, "edit_question GET did not prefill text"
    assert re.search(r'value="4"', edit_html), "edit_question GET did not prefill option_b"
    assert re.search(r'value="B"[^>]*checked|checked[^>]*value="B"', edit_html), \
        "edit_question GET did not pre-check correct_option radio"
    print("  ✓ edit_question prefills text/option/correct_option")

    for method, path in [
        ("get", f"/quiz/{set_id}/edit"),
        ("get", f"/quiz/{set_id}/questions/{question_id}/edit"),
    ]:
        r = other.request(method, f"{BASE}{path}", allow_redirects=False)
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}, expected 403"
    r = other.post(f"{BASE}/quiz/{set_id}/questions/{question_id}/delete",
                    data={"csrf_token": csrf(other, f"{BASE}/quiz/")}, allow_redirects=False)
    assert r.status_code == 403, f"delete_question -> {r.status_code}, expected 403"
    print("  ✓ cross-user edit/delete all return 403")

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
