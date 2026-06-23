import os
import sys
import time
import random
import gspread
from google.oauth2.service_account import Credentials

class SheetsService:
    """Serviço para gerenciar conexão e manipulação da planilha Google Sheets."""
    def __init__(self, credentials_path, sheets_url, log_callback):
        self.credentials_path = credentials_path
        self.sheets_url = sheets_url
        self.log_callback = log_callback
        self.client = None
        self.worksheet = None
        import threading
        self.lock = threading.Lock()

    def conectar(self):
        self.log_callback("[SHEETS] Iniciando autenticacao na API do Google Sheets...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if not os.path.exists(self.credentials_path):
            raise FileNotFoundError("Arquivo credentials.json nao encontrado!")
        
        credenciais = Credentials.from_service_account_file(self.credentials_path, scopes=scopes)
        self.client = gspread.authorize(credenciais)
        self.worksheet = self.client.open_by_url(self.sheets_url).get_worksheet(0)
        self.log_callback("[OK] Planilha conectada com sucesso.")

    def obter_dados(self):
        with self.lock:
            return self.worksheet.get_all_values()

    def atualizar_celulas(self, cells_to_update, max_tentativas=5):
        """Atualiza células no Google Sheets com retentativas e backoff exponencial."""
        for tentativa in range(1, max_tentativas + 1):
            try:
                with self.lock:
                    self.worksheet.update_cells(cells_to_update)
                return True
            except Exception as e:
                if tentativa == max_tentativas:
                    self.log_callback(f"[SHEETS] [ERRO CRITICO] Falha ao gravar dados apos {max_tentativas} tentativas: {str(e)}")
                    raise e
                
                # Backoff exponencial com jitter
                wait_time = (2 ** tentativa) + random.uniform(0.1, 1.0)
                self.log_callback(f"[SHEETS] [AVISO] Limite de cota ou erro de conexao. Re-tentando em {wait_time:.1f} segundos (Tentativa {tentativa}/{max_tentativas})...")
                time.sleep(wait_time)
