from io import BytesIO

from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle


def demo_pdf():
    output = BytesIO()
    pdf = canvas.Canvas(output, invariant=True)
    pdf.setTitle("Retrieval Evaluation — Synthetic Demo")
    pdf.setAuthor("CyberGarden demo")
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(48, 782, "Retrieval evaluation")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(48, 753, "Synthetic publication for checking extraction and validation.")
    pdf.drawString(48, 710, "Using BM25, MRR@10 was 42% on ExampleQA.")
    pdf.drawString(48, 676, "Table 1. Results by retriever on ExampleQA.")
    table = Table(
        [["Retriever", "Dataset", "Recall@5 (%)", "Recall@10 (%)", "nDCG@10"],
         ["BM25", "ExampleQA", "60", "72", "0.55"],
         ["Dense", "ExampleQA", "81", "70", "0.68"]],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.7, colors.darkgray),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    _, height = table.wrapOn(pdf, 500, 400)
    table.drawOn(pdf, 48, 658 - height)
    pdf.drawString(48, 537, "Figure 1. Accuracy reached 85% on ExampleQA.")
    pdf.drawString(48, 500, "Recall = relevant_retrieved / relevant_total")
    pdf.save()
    return output.getvalue()
