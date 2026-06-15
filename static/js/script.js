document.addEventListener('DOMContentLoaded', () => {
    const socket          = io();
    const btnIniciar      = document.getElementById('btn-iniciar');
    const btnContinuar    = document.getElementById('btn-continuar');
    const btnLimparProg   = document.getElementById('btn-limpar-prog');
    const consoleLogs     = document.getElementById('console-logs');
    const progressContainer = document.getElementById('progress-container');
    const progressBar     = document.getElementById('progress-bar');
    const progressText    = document.getElementById('progress-text');
    const progressPercent = document.getElementById('progress-percent');
    const bannerProgresso = document.getElementById('banner-progresso');
    const bannerTexto     = document.getElementById('banner-texto');

    // ─── Verifica se há progresso salvo ao carregar a página ─────────────
    fetch('/api/progresso')
        .then(r => r.json())
        .then(data => {
            if (data.tem_progresso) {
                bannerTexto.textContent =
                    `Execução anterior interrompida: ${data.processados} de ${data.total} chamados processados.`;
                bannerProgresso.style.display = 'flex';
                btnContinuar.textContent = `↩ Continuar (${data.processados}/${data.total})`;
                btnContinuar.style.display = 'block';
            }
        })
        .catch(() => {}); // silencia erro se servidor ainda não está pronto

    // ─── Classifica a linha de log pela sua tag ──────────────────────────
    function classificarLog(texto) {
        const t = texto.toLowerCase();
        if (t.includes('[ok]'))                          return 'log-ok';
        if (t.includes('[erro') || t.includes('erro:')) return 'log-erro';
        if (t.includes('[aviso]') || t.includes('[nao localizado]') ||
            t.includes('[timeout]'))                     return 'log-aviso';
        if (t.includes('[plano b]'))                     return 'log-plano-b';
        if (t.includes('[fim]') || t.includes('[inicio]') ||
            t.includes('[sheets]') || t.includes('[ca sdm]') ||
            t.includes('[login]'))                       return 'log-info';
        if (t.startsWith('===') || t.startsWith('---')) return 'log-sistema';
        return 'log-default';
    }

    // ─── Adiciona uma linha ao console ───────────────────────────────────
    function adicionarLog(texto, classeExtra) {
        const div = document.createElement('div');
        div.className = 'log-line ' + (classeExtra || classificarLog(texto));
        div.textContent = '> ' + texto;
        consoleLogs.appendChild(div);
        consoleLogs.scrollTop = consoleLogs.scrollHeight;
    }

    // ─── Bloqueia/desbloqueia os botões ──────────────────────────────────
    function setBotoes(processando) {
        btnIniciar.disabled   = processando;
        btnContinuar.disabled = processando;
        btnIniciar.textContent = processando
            ? 'Processando Conferência...'
            : 'Iniciar Conferência';
        if (!processando) {
            btnIniciar.style.background = '';
            btnIniciar.style.cursor     = '';
        }
    }

    function limparProgresso() {
        progressBar.style.width = '0%';
        progressText.textContent = '';
        progressPercent.textContent = '0%';
        progressContainer.style.display = 'none';
        bannerProgresso.style.display  = 'none';
        btnContinuar.style.display     = 'none';
    }

    // ─── Botão: Iniciar (do zero) ────────────────────────────────────────
    btnIniciar.addEventListener('click', () => {
        setBotoes(true);
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressText.textContent = 'Iniciando...';
        progressPercent.textContent = '0%';
        consoleLogs.innerHTML = '<div class="log-line log-sistema">> Solicitação enviada ao servidor...</div>';
        socket.emit('iniciar_conferencia');
    });

    // ─── Botão: Continuar de onde parou ──────────────────────────────────
    btnContinuar.addEventListener('click', () => {
        setBotoes(true);
        progressContainer.style.display = 'block';
        consoleLogs.innerHTML = '<div class="log-line log-sistema">> Retomando execução anterior...</div>';
        socket.emit('continuar_conferencia');
    });

    // ─── Botão: Descartar progresso salvo ────────────────────────────────
    btnLimparProg.addEventListener('click', () => {
        socket.emit('limpar_progresso');
    });

    // ─── Eventos do servidor ─────────────────────────────────────────────

    socket.on('log_message', (msg) => {
        adicionarLog(msg.data);

        // Detecta fim da varredura para reativar botões
        const t = msg.data.toLowerCase();
        if (t.includes('[fim]') || t.includes('varredura concluida') ||
            t.includes('[erro critico]')) {
            setBotoes(false);
        }
    });

    socket.on('progresso', (data) => {
        const { atual, total } = data;
        const pct = total > 0 ? Math.round((atual / total) * 100) : 0;
        progressBar.style.width = pct + '%';
        progressText.textContent = `Processando chamado ${atual} de ${total}`;
        progressPercent.textContent = pct + '%';
    });

    socket.on('automacao_bloqueada', () => {
        setBotoes(false); // já estava processando, só reativa
    });

    socket.on('automacao_concluida', () => {
        setBotoes(false);
    });

    socket.on('progresso_limpo', () => {
        limparProgresso();
        adicionarLog('Progresso anterior descartado.', 'log-sistema');
    });
});
