import sqlite3
import os
from datetime import datetime

DB_PATH = 'historico.db'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def inicializar_db():
    """Cria as tabelas caso não existam no SQLite e popula dados iniciais."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Tabela de execuções
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS execucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_inicio TEXT,
            tempo_total REAL,
            total_chamados INTEGER,
            sucessos INTEGER,
            avisos INTEGER,
            erros INTEGER,
            col_d INTEGER,
            col_e INTEGER,
            col_g INTEGER
        )
    ''')
    
    # Tabela de grupos desconhecidos
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grupos_desconhecidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucao_id INTEGER,
            grupo_raw TEXT,
            FOREIGN KEY (execucao_id) REFERENCES execucoes(id) ON DELETE CASCADE
        )
    ''')
    
    # Tabela de mapeamentos de torre dinâmica (substring matching)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mapeamento_torres (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grupo_match TEXT UNIQUE,
            torre TEXT
        )
    ''')
    
    # Tabela de auditoria de erros com print base64
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS erros_detalhes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucao_id INTEGER,
            linha_planilha INTEGER,
            id_chamado TEXT,
            mensagem_erro TEXT,
            screenshot_base64 TEXT,
            FOREIGN KEY (execucao_id) REFERENCES execucoes(id) ON DELETE CASCADE
        )
    ''')
    
    # Popula dados iniciais de mapeamento se a tabela estiver vazia
    cursor.execute('SELECT COUNT(*) as count FROM mapeamento_torres')
    if cursor.fetchone()['count'] == 0:
        default_mappings = [
            ('SERVICE DESK NIVEL', 'N1'),
            ('TORRE A', 'A'),
            ('TORRE B', 'B'),
            ('TORRE C', 'C'),
            ('COEIN', 'COEIN'),
            ('GESTAO DE DADOS', 'BI'),
            ('GESTÃO DE DADOS', 'BI'),
            ('BI', 'BI')
        ]
        cursor.executemany('''
            INSERT INTO mapeamento_torres (grupo_match, torre)
            VALUES (?, ?)
        ''', default_mappings)
    
    conn.commit()
    conn.close()

def criar_execucao(data_inicio):
    """Insere o início de uma execução e retorna seu ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO execucoes (data_inicio, tempo_total, total_chamados, sucessos, avisos, erros, col_d, col_e, col_g)
        VALUES (?, 0.0, 0, 0, 0, 0, 0, 0, 0)
    ''', (data_inicio,))
    exec_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return exec_id

def atualizar_execucao(exec_id, tempo_total, total_chamados, sucessos, avisos, erros, col_d, col_e, col_g):
    """Atualiza as estatísticas finais da execução."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE execucoes
        SET tempo_total = ?, total_chamados = ?, sucessos = ?, avisos = ?, erros = ?, col_d = ?, col_e = ?, col_g = ?
        WHERE id = ?
    ''', (tempo_total, total_chamados, sucessos, avisos, erros, col_d, col_e, col_g, exec_id))
    conn.commit()
    conn.close()

def registrar_grupo_desconhecido(exec_id, grupo_raw):
    """Registra um grupo de suporte que não pôde ser mapeado."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM grupos_desconhecidos WHERE execucao_id = ? AND grupo_raw = ?', (exec_id, grupo_raw))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO grupos_desconhecidos (execucao_id, grupo_raw)
            VALUES (?, ?)
        ''', (exec_id, grupo_raw))
        conn.commit()
    conn.close()

def registrar_erro_detalhado(execucao_id, linha_planilha, id_chamado, mensagem_erro, screenshot_base64):
    """Registra um erro detalhado ocorrido durante a execução com captura de tela."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO erros_detalhes (execucao_id, linha_planilha, id_chamado, mensagem_erro, screenshot_base64)
        VALUES (?, ?, ?, ?, ?)
    ''', (execucao_id, linha_planilha, id_chamado, mensagem_erro, screenshot_base64))
    conn.commit()
    conn.close()

def listar_erros_execucao(execucao_id):
    """Retorna os detalhes de erros e capturas de tela associados a uma execução."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, linha_planilha, id_chamado, mensagem_erro, screenshot_base64
        FROM erros_detalhes
        WHERE execucao_id = ?
        ORDER BY id ASC
    ''', (execucao_id,))
    rows = cursor.fetchall()
    erros = [dict(r) for r in rows]
    conn.close()
    return erros

def listar_historico():
    """Retorna a lista de todas as execuções salvas com seus grupos desconhecidos."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM execucoes ORDER BY id DESC')
    rows = cursor.fetchall()
    
    historico = []
    for r in rows:
        exec_id = r['id']
        cursor.execute('SELECT grupo_raw FROM grupos_desconhecidos WHERE execucao_id = ?', (exec_id,))
        grupos = [g['grupo_raw'] for g in cursor.fetchall()]
        
        historico.append({
            'id': r['id'],
            'data_inicio': r['data_inicio'],
            'tempo_total': r['tempo_total'],
            'total_chamados': r['total_chamados'],
            'sucessos': r['sucessos'],
            'avisos': r['avisos'],
            'erros': r['erros'],
            'col_d': r['col_d'],
            'col_e': r['col_e'],
            'col_g': r['col_g'],
            'grupos_desconhecidos': grupos
        })
        
    conn.close()
    return historico

# ─── Funções CRUD para Mapeamento de Torres ──────────────────────────────

def listar_mapeamentos():
    """Retorna todos os mapeamentos cadastrados no SQLite."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM mapeamento_torres ORDER BY grupo_match ASC')
    rows = cursor.fetchall()
    mapeamentos = [dict(r) for r in rows]
    conn.close()
    return mapeamentos

def inserir_mapeamento(grupo_match, torre):
    """Adiciona ou atualiza uma regra de mapeamento."""
    conn = get_db_connection()
    cursor = conn.cursor()
    grupo_clean = grupo_match.strip().upper()
    torre_clean = torre.strip().upper()
    try:
        cursor.execute('''
            INSERT INTO mapeamento_torres (grupo_match, torre)
            VALUES (?, ?)
            ON CONFLICT(grupo_match) DO UPDATE SET torre = excluded.torre
        ''', (grupo_clean, torre_clean))
        conn.commit()
        success = True
    except Exception:
        success = False
    conn.close()
    return success

def deletar_mapeamento(mapping_id):
    """Exclui uma regra de mapeamento pelo ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM mapeamento_torres WHERE id = ?', (mapping_id,))
    conn.commit()
    conn.close()
