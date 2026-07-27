import os
from dotenv import load_dotenv

load_dotenv()

from app import create_app, socketio

app = create_app(os.environ.get('FLASK_CONFIG', 'default'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host='0.0.0.0', port=port, debug=True, allow_unsafe_werkzeug=True)
