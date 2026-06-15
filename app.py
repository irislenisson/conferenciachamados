import os
import json
import threading
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
from scraper import iniciar_automacao

PROGRESS_FILE = 'progresso.json'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev_troque_no_env')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ─── Flag global: impede execuções simultâneas ───────────────────────────────
_automacao_em_andamento = False


def _carregar_progresso():
    """Lê o arquivo de progresso salvo de uma execução interrompida."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/progresso')
def api_progresso():
    """Endpoint REST: informa se há progresso salvo de uma execução anterior."""
    info = _carregar_progresso()
    if info and info.get('processados'):
        return jsonify({
            'tem_progresso': True,
            'processados': len(info['processados']),
            'total': info.get('total', '?')
        })
    return jsonify({'tem_progresso': False})


def _roda_thread(ja_processados, headless, num_threads):
    """Função executada na thread de automação. Garante liberação da flag ao final."""
    global _automacao_em_andamento
    try:
        iniciar_automacao(
            socketio_emit_callback=socketio.emit,
            ja_processados=ja_processados,
            headless=headless,
            num_threads=num_threads
        )
    finally:
        _automacao_em_andamento = False
        socketio.emit('automacao_concluida', {})


@socketio.on('iniciar_conferencia')
def handle_iniciar(data=None):
    """Inicia uma nova varredura do zero. Apaga progresso anterior."""
    global _automacao_em_andamento
    if _automacao_em_andamento:
        socketio.emit('log_message', {'data': '[AVISO] Automacao ja em andamento. Aguarde a conclusao.'})
        socketio.emit('automacao_bloqueada', {})
        return
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    _automacao_em_andamento = True
    
    data = data or {}
    headless = data.get('headless', True)
    num_threads = data.get('num_threads', 1)
    
    socketio.emit('log_message', {'data': f'[INICIO] Iniciando nova varredura do zero (Modo Invisivel={headless}, Navegadores={num_threads})...'})
    threading.Thread(target=_roda_thread, args=(set(), headless, num_threads), daemon=True).start()


@socketio.on('continuar_conferencia')
def handle_continuar(data=None):
    """Continua uma varredura interrompida, pulando chamados já processados."""
    global _automacao_em_andamento
    if _automacao_em_andamento:
        socketio.emit('log_message', {'data': '[AVISO] Automacao ja em andamento.'})
        socketio.emit('automacao_bloqueada', {})
        return
    info = _carregar_progresso()
    ja_processados = set(info['processados']) if info else set()
    _automacao_em_andamento = True
    n = len(ja_processados)
    
    data = data or {}
    headless = data.get('headless', True)
    num_threads = data.get('num_threads', 1)
    
    socketio.emit('log_message', {'data': f'[INICIO] Continuando varredura ({n} chamado(s) ja processados, Modo Invisivel={headless}, Navegadores={num_threads})...'})
    threading.Thread(target=_roda_thread, args=(ja_processados, headless, num_threads), daemon=True).start()


@socketio.on('limpar_progresso')
def handle_limpar():
    """Remove o arquivo de progresso salvo."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    socketio.emit('progresso_limpo', {})


if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000, allow_unsafe_werkzeug=True)
