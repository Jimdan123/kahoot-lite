# gevent's monkey.patch_all() must run before any other import — see
# wsgi.py for the full explanation. Flask-SocketIO auto-picks gevent as
# its async_mode whenever it's importable (it's a hard dependency here,
# for the production Docker deploy), so this dev entrypoint needs the
# same patch as wsgi.py or a blocking call (e.g. the AI pipeline's LLM
# API requests) freezes the whole single-threaded dev server.
from gevent import monkey
monkey.patch_all()

import os                            # noqa: E402
from dotenv import load_dotenv       # noqa: E402

load_dotenv()

from app import create_app, socketio  # noqa: E402

app = create_app(os.environ.get('FLASK_CONFIG', 'default'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    # use_reloader=False: Werkzeug's reloader restarts the app by spawning a
    # child process, which crashes against gevent's monkey-patched threading
    # (AssertionError in gevent's after_fork_in_child). debug=True still
    # gives the interactive debugger on unhandled exceptions — just no
    # auto-restart on file save.
    socketio.run(app, host='0.0.0.0', port=port, debug=True,
                 use_reloader=False, allow_unsafe_werkzeug=True)
