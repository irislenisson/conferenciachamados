document.addEventListener('DOMContentLoaded', () => {
    const socket            = io();
    const btnIniciar        = document.getElementById('btn-iniciar');
    const btnContinuar      = document.getElementById('btn-continuar');
    const btnLimparProg     = document.getElementById('btn-limpar-prog');
    const consoleLogs       = document.getElementById('console-logs');
    const progressContainer = document.getElementById('progress-container');
    const progressBar       = document.getElementById('progress-bar');
    const progressText      = document.getElementById('progress-text');
    const progressPercent   = document.getElementById('progress-percent');
    const bannerProgresso   = document.getElementById('banner-progresso');
    const bannerTexto       = document.getElementById('banner-texto');
    const chkHeadless       = document.getElementById('chk-headless');
    const selectThreads     = document.getElementById('select-threads');
    const warningParallel   = document.getElementById('warning-parallel');
    const btnExportar       = document.getElementById('btn-exportar');

    // Novos Elementos
    const themeToggle       = document.getElementById('theme-toggle');
    const wsStatusDot       = document.getElementById('ws-status-dot');
    const wsStatusText      = document.getElementById('ws-status-text');
    const inputWebhook      = document.getElementById('webhook-url');
    const inputTimeoutBusca = document.getElementById('timeout-busca');
    const inputTimeoutPagina = document.getElementById('timeout-pagina');
    
    // Métricas do Dashboard
    const metricTotal       = document.getElementById('metric-total');
    const metricSucesso     = document.getElementById('metric-sucesso');
    const metricAviso       = document.getElementById('metric-aviso');
    const metricErro        = document.getElementById('metric-erro');
    const metricTempo       = document.getElementById('metric-tempo');
    const metricUpdates     = document.getElementById('metric-updates');
    
    // Tabela de Histórico
    const historicoTabelaCorpo = document.getElementById('historico-tabela-corpo');
    
    // Busca e Filtros de Console
    const logSearch         = document.getElementById('log-search');
    const filterButtons     = document.querySelectorAll('.btn-filter');

    let currentLogFilter = 'all'; // 'all', 'ok', 'aviso', 'erro'
    let currentLogSearch = '';
    
    // Contadores locais para atualizar dashboard em tempo real
    let countTotal = 0;
    let countSucesso = 0;
    let countAviso = 0;
    let countErro = 0;
    let countColD = 0;
    let countColE = 0;
    let countColG = 0;

    // ─── 1. Gerenciamento de Tema (Dark/Light) ───────────────────────────
    const applyTheme = (theme) => {
        if (theme === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
            themeToggle.querySelector('.theme-icon').textContent = '☀️';
        } else {
            document.documentElement.removeAttribute('data-theme');
            themeToggle.querySelector('.theme-icon').textContent = '🌙';
        }
        localStorage.setItem('theme', theme);
    };

    const savedTheme = localStorage.getItem('theme') || 'light';
    applyTheme(savedTheme);

    themeToggle.addEventListener('click', () => {
        const currentTheme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
        applyTheme(nextTheme);
    });

    // ─── 2. Persistência de Configurações no LocalStorage ────────────────
    const carregarConfiguracoes = () => {
        if (localStorage.getItem('webhook_url') !== null) {
            inputWebhook.value = localStorage.getItem('webhook_url');
        }
        if (localStorage.getItem('timeout_busca') !== null) {
            inputTimeoutBusca.value = localStorage.getItem('timeout_busca');
        }
        if (localStorage.getItem('timeout_pagina') !== null) {
            inputTimeoutPagina.value = localStorage.getItem('timeout_pagina');
        }
        if (localStorage.getItem('headless') !== null) {
            chkHeadless.checked = localStorage.getItem('headless') === 'true';
        }
        if (localStorage.getItem('num_threads') !== null) {
            selectThreads.value = localStorage.getItem('num_threads');
            triggerParallelWarning();
        }
    };

    const salvarConfiguracoes = () => {
        localStorage.setItem('webhook_url', inputWebhook.value);
        localStorage.setItem('timeout_busca', inputTimeoutBusca.value);
        localStorage.setItem('timeout_pagina', inputTimeoutPagina.value);
        localStorage.setItem('headless', chkHeadless.checked);
        localStorage.setItem('num_threads', selectThreads.value);
    };

    const triggerParallelWarning = () => {
        if (parseInt(selectThreads.value) > 1) {
            warningParallel.style.display = 'block';
        } else {
            warningParallel.style.display = 'none';
        }
    };

    selectThreads.addEventListener('change', () => {
        triggerParallelWarning();
        salvarConfiguracoes();
    });
    chkHeadless.addEventListener('change', salvarConfiguracoes);
    inputWebhook.addEventListener('input', salvarConfiguracoes);
    inputTimeoutBusca.addEventListener('input', salvarConfiguracoes);
    inputTimeoutPagina.addEventListener('input', salvarConfiguracoes);

    carregarConfiguracoes();

    // ─── 3. Monitoramento de WebSocket (Online/Offline) ────────────────
    socket.on('connect', () => {
        wsStatusDot.className = 'status-dot online';
        wsStatusText.textContent = 'Conectado';
    });

    socket.on('disconnect', () => {
        wsStatusDot.className = 'status-dot offline';
        wsStatusText.textContent = 'Desconectado';
    });

    socket.on('connect_error', () => {
        wsStatusDot.className = 'status-dot offline';
        wsStatusText.textContent = 'Erro de Conexão';
    });

    // ─── 4. Histórico SQLite (Carregar e Exibir) ───────────────────────
    const carregarHistorico = () => {
        fetch('/api/historico')
            .then(r => r.json())
            .then(dados => {
                if (!dados || dados.length === 0) {
                    historicoTabelaCorpo.innerHTML = `
                        <tr>
                            <td colspan="6" class="text-center">Nenhuma execução registrada no banco.</td>
                        </tr>`;
                    return;
                }
                
                historicoTabelaCorpo.innerHTML = '';
                dados.forEach(exec => {
                    const tr = document.createElement('tr');
                    tr.style.cursor = 'pointer';
                    tr.title = 'Clique para carregar esta execução no dashboard';
                    
                    const totalSecs = parseFloat(exec.tempo_total) || 0;
                    const tempoFormat = totalSecs > 60 
                        ? `${Math.floor(totalSecs/60)}m ${Math.round(totalSecs%60)}s` 
                        : `${totalSecs.toFixed(1)}s`;

                    tr.innerHTML = `
                        <td><strong>#${exec.id}</strong></td>
                        <td>${exec.data_inicio}</td>
                        <td>${exec.total_chamados}</td>
                        <td>
                            <span class="log-ok">${exec.sucessos}</span> / 
                            <span class="log-aviso">${exec.avisos}</span> / 
                            <span class="log-erro">${exec.erros}</span>
                        </td>
                        <td>${tempoFormat}</td>
                        <td>D: ${exec.col_d} • E: ${exec.col_e} • G: ${exec.col_g}</td>
                    `;
                    
                    // Permite que o usuário clique para rever as métricas antigas no dashboard
                    tr.addEventListener('click', () => {
                        exibirMetricasNoDashboard({
                            total: exec.total_chamados,
                            sucessos: exec.sucessos,
                            avisos: exec.avisos,
                            erros: exec.erros,
                            tempo: tempoFormat,
                            col_d: exec.col_d,
                            col_e: exec.col_e,
                            col_g: exec.col_g
                        });
                    });
                    
                    historicoTabelaCorpo.appendChild(tr);
                });
            })
            .catch(err => {
                console.error("Erro ao ler historico:", err);
            });
    };

    const exibirMetricasNoDashboard = (m) => {
        metricTotal.textContent = m.total;
        metricSucesso.textContent = m.sucessos;
        metricAviso.textContent = m.avisos;
        metricErro.textContent = m.erros;
        metricTempo.textContent = m.tempo;
        metricUpdates.innerHTML = `Colunas atualizadas: <span>D: ${m.col_d}</span> • <span>E: ${m.col_e}</span> • <span>G: ${m.col_g}</span>`;
    };

    // Carrega o histórico assim que abre a página
    carregarHistorico();

    // ─── 5. Resetar Métricas do Dashboard para Iniciar Run ──────────────
    const resetarContadoresDashboard = () => {
        countTotal = 0;
        countSucesso = 0;
        countAviso = 0;
        countErro = 0;
        countColD = 0;
        countColE = 0;
        countColG = 0;
        
        exibirMetricasNoDashboard({
            total: 0,
            sucessos: 0,
            avisos: 0,
            erros: 0,
            tempo: '0.0s',
            col_d: 0,
            col_e: 0,
            col_g: 0
        });
    };

    // ─── 6. Análise de Logs em Tempo Real (Atualização do Dashboard) ─────
    const analisarMensagemDeLog = (texto) => {
        const t = texto.toLowerCase();

        // Detecta total de pendentes
        if (t.includes('total de chamados pendentes na planilha:')) {
            const match = texto.match(/planilha:\s*(\d+)/i);
            if (match) {
                countTotal = parseInt(match[1]);
                metricTotal.textContent = countTotal;
            }
        }
        
        // Detecta incrementos nas colunas atualizadas
        if (t.includes('[ok] coluna d')) {
            countColD++;
            countSucesso++;
        }
        if (t.includes('[ok] coluna e')) {
            countColE++;
            countSucesso++;
        }
        if (t.includes('[ok] coluna g')) {
            countColG++;
            countSucesso++;
        }
        if (t.includes('verificado sem pendências') || t.includes('coluna d ja correta') || t.includes('coluna e ja preenchida')) {
            countSucesso++;
        }

        // Detecta Avisos
        if (t.includes('[aviso]') || t.includes('[nao localizado]') || t.includes('[timeout]')) {
            countAviso++;
        }

        // Detecta Erros
        if (t.includes('[erro') || t.includes('erro:')) {
            countErro++;
        }

        // Atualiza dashboard em tempo real
        metricSucesso.textContent = countSucesso;
        metricAviso.textContent = countAviso;
        metricErro.textContent = countErro;
        metricUpdates.innerHTML = `Colunas atualizadas: <span>D: ${countColD}</span> • <span>E: ${countColE}</span> • <span>G: ${countColG}</span>`;
    };

    // ─── 7. Busca e Filtragem Avançada de Logs no Console ────────────────
    const filtrarConsoleLogs = () => {
        const lines = consoleLogs.querySelectorAll('.log-line');
        lines.forEach(div => {
            const textMatches = currentLogSearch === '' || div.textContent.toLowerCase().includes(currentLogSearch);
            let classMatches = true;

            if (currentLogFilter === 'ok') {
                classMatches = div.classList.contains('log-ok');
            } else if (currentLogFilter === 'aviso') {
                classMatches = div.classList.contains('log-aviso');
            } else if (currentLogFilter === 'erro') {
                classMatches = div.classList.contains('log-erro');
            }

            if (textMatches && classMatches) {
                div.style.display = 'block';
            } else {
                div.style.display = 'none';
            }
        });
    };

    logSearch.addEventListener('input', () => {
        currentLogSearch = logSearch.value.trim().toLowerCase();
        filtrarConsoleLogs();
    });

    filterButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            filterButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentLogFilter = btn.getAttribute('data-filter');
            filtrarConsoleLogs();
        });
    });

    // ─── 8. Tratamento Semântico de Classes de Log ───────────────────────
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

    function adicionarLog(texto, classeExtra) {
        const div = document.createElement('div');
        div.className = 'log-line ' + (classeExtra || classificarLog(texto));
        div.textContent = '> ' + texto;
        consoleLogs.appendChild(div);
        
        // Aplica filtros correntes à nova linha
        const textMatches = currentLogSearch === '' || div.textContent.toLowerCase().includes(currentLogSearch);
        let classMatches = true;
        if (currentLogFilter === 'ok') classMatches = div.classList.contains('log-ok');
        else if (currentLogFilter === 'aviso') classMatches = div.classList.contains('log-aviso');
        else if (currentLogFilter === 'erro') classMatches = div.classList.contains('log-erro');
        
        div.style.display = (textMatches && classMatches) ? 'block' : 'none';
        consoleLogs.scrollTop = consoleLogs.scrollHeight;
    }

    // ─── 9. Bloqueio e Liberação de UI ───────────────────────────────────
    function setBotoes(processando) {
        btnIniciar.disabled   = processando;
        btnContinuar.disabled = processando;
        btnIniciar.textContent = processando
            ? 'Processando...'
            : 'Iniciar Conferência';
    }

    function limparProgresso() {
        progressBar.style.width = '0%';
        progressText.textContent = '';
        progressPercent.textContent = '0%';
        progressContainer.style.display = 'none';
        bannerProgresso.style.display  = 'none';
        btnContinuar.style.display     = 'none';
    }

    // Verifica progresso salvo
    const verificarProgressoServidor = () => {
        fetch('/api/progresso')
            .then(r => r.json())
            .then(data => {
                if (data.tem_progresso) {
                    bannerTexto.textContent =
                        `Execução incompleta: ${data.processados} de ${data.total} chamados processados.`;
                    bannerProgresso.style.display = 'flex';
                    btnContinuar.textContent = `↩ Continuar (${data.processados}/${data.total})`;
                    btnContinuar.style.display = 'block';
                }
            })
            .catch(() => {});
    };

    verificarProgressoServidor();

    // ─── 10. Botão: Iniciar (do zero) ────────────────────────────────────
    btnIniciar.addEventListener('click', () => {
        setBotoes(true);
        resetarContadoresDashboard();
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressText.textContent = 'Iniciando...';
        progressPercent.textContent = '0%';
        consoleLogs.innerHTML = '<div class="log-line log-sistema">> Enviando solicitacao ao servidor...</div>';
        
        socket.emit('iniciar_conferencia', {
            headless: chkHeadless.checked,
            num_threads: parseInt(selectThreads.value),
            webhook_url: inputWebhook.value,
            timeout_busca: parseInt(inputTimeoutBusca.value),
            timeout_pagina: parseInt(inputTimeoutPagina.value)
        });
    });

    // ─── 11. Botão: Continuar de onde parou ──────────────────────────────
    btnContinuar.addEventListener('click', () => {
        setBotoes(true);
        progressContainer.style.display = 'block';
        consoleLogs.innerHTML = '<div class="log-line log-sistema">> Retomando execucao anterior...</div>';
        
        socket.emit('continuar_conferencia', {
            headless: chkHeadless.checked,
            num_threads: parseInt(selectThreads.value),
            webhook_url: inputWebhook.value,
            timeout_busca: parseInt(inputTimeoutBusca.value),
            timeout_pagina: parseInt(inputTimeoutPagina.value)
        });
    });

    // ─── 12. Botão: Descartar progresso salvo ────────────────────────────
    btnLimparProg.addEventListener('click', () => {
        socket.emit('limpar_progresso');
    });

    // ─── 13. Botão: Exportar Logs ────────────────────────────────────────
    btnExportar.addEventListener('click', () => {
        const lines = Array.from(consoleLogs.querySelectorAll('.log-line'))
            .map(div => div.textContent.replace(/^>\s*/, ''))
            .join('\r\n');
        
        const blob = new Blob([lines], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `logs_conferencia_${new Date().toISOString().slice(0,19).replace(/[:T]/g, '_')}.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });

    // ─── 14. Eventos do Servidor (Socket.IO) ─────────────────────────────
    socket.on('log_message', (msg) => {
        adicionarLog(msg.data);
        analisarMensagemDeLog(msg.data);

        // Reativa os botões quando a varredura termina
        const t = msg.data.toLowerCase();
        if (t.includes('[fim]') || t.includes('varredura concluida') || t.includes('[erro critico]')) {
            setBotoes(false);
            carregarHistorico(); // Recarrega a tabela de histórico SQLite
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
        setBotoes(false);
    });

    socket.on('automacao_concluida', () => {
        setBotoes(false);
        carregarHistorico();
    });

    socket.on('progresso_limpo', () => {
        limparProgresso();
        adicionarLog('Progresso anterior descartado.', 'log-sistema');
    });
});
