"""Lapisan layanan: logika bisnis yang dipakai oleh route."""


class ValidationError(Exception):
    """Data dari formulir tidak valid.

    errors berisi {nama_field: pesan}. Kunci "_" dipakai untuk pesan umum
    yang tidak terkait satu field tertentu.
    """

    def __init__(self, errors):
        super().__init__("Data tidak valid")
        self.errors = errors
