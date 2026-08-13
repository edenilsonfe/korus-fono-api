"""PDF generator for non-fiscal, internal payment acknowledgements."""

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.models.finance import FinancialPayment, FinancialProfile


def _money(cents: int) -> str:
    value = f"{cents / 100:,.2f}"
    return f"R$ {value.replace(',', 'X').replace('.', ',').replace('X', '.')}"


def _single_line(value: str, limit: int = 100) -> str:
    return " ".join(value.split())[:limit]


def build_internal_receipt(payment: FinancialPayment, profile: FinancialProfile | None) -> bytes:
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    width, height = A4
    issuer = (profile.trade_name or profile.legal_name) if profile else "Profissional responsável"
    pdf.setTitle(f"Comprovante interno {payment.receipt_number}")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(56, height - 70, "Comprovante interno de recebimento")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(56, height - 94, f"Número: {payment.receipt_number}")
    pdf.drawString(56, height - 112, f"Emitente: {_single_line(issuer, 80)}")
    if profile and profile.document:
        pdf.drawString(56, height - 130, f"Documento do emitente: {_single_line(profile.document, 20)}")
    pdf.drawString(56, height - 148, f"Pagador: {_single_line(payment.payer_name, 80)}")
    if payment.payer_document:
        pdf.drawString(56, height - 166, f"Documento do pagador: {_single_line(payment.payer_document, 20)}")
    pdf.drawString(56, height - 184, f"Data do recebimento: {payment.payment_date:%d/%m/%Y}")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(56, height - 218, f"Valor recebido: {_money(payment.amount_cents)}")
    detail_y = height - 244
    if payment.status == "reversed":
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(56, detail_y, "RECEBIMENTO ESTORNADO")
        detail_y -= 22
        if payment.reversal_reason:
            pdf.setFont("Helvetica", 9)
            pdf.drawString(56, detail_y, f"Motivo: {_single_line(payment.reversal_reason)}")
            detail_y -= 18
    if payment.notes:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(56, detail_y, f"Observações: {_single_line(payment.notes)}")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(56, 72, "Documento de controle interno. Não substitui recibo fiscal, Receita Saúde ou NFS-e.")
    pdf.setFont("Helvetica", 8)
    pdf.drawString(56, 56, "A validade fiscal deve ser tratada no sistema oficial aplicável ao emitente.")
    pdf.showPage()
    pdf.save()
    return output.getvalue()
