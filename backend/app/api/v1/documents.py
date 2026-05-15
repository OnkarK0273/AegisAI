from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from typing import List
from io import BytesIO
import re

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.ai_system import AISystem
from app.models.document import Document, DocumentType, DocumentStatus
from app.schemas.document import DocumentCreate, DocumentResponse, DocumentGenerateRequest, DocumentUpdateRequest

# PDF generation
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER

router = APIRouter()

from app.modules.llm.document_generator import generate_compliance_narrative

@router.post("/", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
def create_document(
    doc_data: DocumentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new document."""
    document = Document(
        owner_id=current_user.id,
        title=doc_data.title,
        document_type=doc_data.document_type,
        ai_system_id=doc_data.ai_system_id,
        content=doc_data.content
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


@router.get("/", response_model=List[DocumentResponse])
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all documents for the current user."""
    documents = db.query(Document).filter(Document.owner_id == current_user.id).all()
    return documents


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific document."""
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()
    
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found"
        )
    return document

@router.put("/{document_id}", response_model=DocumentResponse)
def update_document(
    document_id: int,
    body: DocumentUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update document content."""
    # Fetch document
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()
    
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found"
        )
    
    # Update content
    document.content = body.content
    db.commit()
    db.refresh(document)
    
    return document

@router.post("/generate", response_model=DocumentResponse)
def generate_document(
    request: DocumentGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Generate a compliance document for an AI system."""
    # Get the AI system
    ai_system = db.query(AISystem).filter(
        AISystem.id == request.ai_system_id,
        AISystem.owner_id == current_user.id
    ).first()
    
    if not ai_system:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI system not found"
        )
    
    # Get latest risk assessment if available
    from app.models.ai_system import RiskAssessment
    assessment = db.query(RiskAssessment).filter(
        RiskAssessment.ai_system_id == ai_system.id
    ).order_by(RiskAssessment.assessed_at.desc()).first()
    
    try:
        content = generate_compliance_narrative(
            document_type=request.document_type,
            ai_system=ai_system,
            risk_assessment=assessment,
            company_name=current_user.company_name
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate document: {str(e)}"
        )
    
    # Create document
    document = Document(
        owner_id=current_user.id,
        ai_system_id=ai_system.id,
        title=f"{request.document_type.value.replace('_', ' ').title()} - {ai_system.name}",
        document_type=request.document_type,
        status=DocumentStatus.GENERATED,
        content=content
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a document."""
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()
    
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found"
        )
    
    db.delete(document)
    db.commit()


@router.get("/{document_id}/pdf")
def export_document_pdf(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export a document as a PDF file.
    
    Returns:
        - Response status 200 with PDF bytes
        - Content-Type: application/pdf
        - File starts with %PDF- magic bytes
        - File size > 1KB
    """
    # Retrieve the document
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()
    
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found"
        )
    
    if not document.content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document has no content to export"
        )
    
    # Generate PDF
    pdf_buffer = BytesIO()
    
    # Create PDF document
    doc = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch,
    )
    
    # Container for PDF elements
    story = []
    
    # Get styles
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor='#1f2937',
        spaceAfter=12,
        alignment=TA_CENTER,
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['BodyText'],
        fontSize=11,
        alignment=TA_LEFT,
        spaceAfter=12,
    )
    
    # Add title
    story.append(Paragraph(document.title, title_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Add metadata
    metadata_style = ParagraphStyle(
        'Metadata',
        parent=styles['Normal'],
        fontSize=9,
        textColor='#6b7280',
        spaceAfter=12,
    )
    story.append(Paragraph(f"<b>Document Type:</b> {document.document_type.value}", metadata_style))
    story.append(Paragraph(f"<b>Status:</b> {document.status.value}", metadata_style))
    story.append(Paragraph(f"<b>Created:</b> {document.created_at.strftime('%Y-%m-%d %H:%M:%S')}", metadata_style))
    story.append(Spacer(1, 0.3*inch))
    
    # Process content - split by lines and handle markdown-like formatting
    content_lines = document.content.split('\n')
    for line in content_lines:
        if not line.strip():
            story.append(Spacer(1, 0.1*inch))
        elif line.startswith('# '):
            # Heading 1
            heading_style = ParagraphStyle(
                'CustomHeading1',
                parent=styles['Heading1'],
                fontSize=16,
                textColor='#1f2937',
                spaceAfter=12,
                spaceBefore=12,
            )
            story.append(Paragraph(line.replace('# ', ''), heading_style))
        elif line.startswith('## '):
            # Heading 2
            heading_style = ParagraphStyle(
                'CustomHeading2',
                parent=styles['Heading2'],
                fontSize=13,
                textColor='#374151',
                spaceAfter=10,
                spaceBefore=10,
            )
            story.append(Paragraph(line.replace('## ', ''), heading_style))
        elif line.startswith('### '):
            # Heading 3
            heading_style = ParagraphStyle(
                'CustomHeading3',
                parent=styles['Heading3'],
                fontSize=11,
                textColor='#4b5563',
                spaceAfter=8,
                spaceBefore=8,
            )
            story.append(Paragraph(line.replace('### ', ''), heading_style))
        elif line.startswith('- '):
            # Bullet point
            story.append(Paragraph('• ' + line.replace('- ', ''), body_style))
        else:
            # Handle inline bold with regex
            processed_line = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', line.strip())
            story.append(Paragraph(processed_line, body_style))
    
    # Build PDF
    doc.build(story)
    
    # Get PDF bytes
    pdf_bytes = pdf_buffer.getvalue()
    
    # Verify PDF is valid (starts with %PDF- magic bytes)
    if not pdf_bytes.startswith(b'%PDF-'):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PDF generation failed - invalid PDF format"
        )
    
    # Verify PDF is larger than 1KB
    if len(pdf_bytes) < 1024:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PDF generation failed - PDF too small"
        )
    
    # Return PDF response
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{document.title}.pdf"'}
    )
