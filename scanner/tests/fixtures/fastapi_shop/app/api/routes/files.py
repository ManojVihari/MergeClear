from fastapi import APIRouter, File, Form, UploadFile

router = APIRouter(tags=["files"])


@router.post("/files")
async def upload(file: UploadFile = File(...), description: str = Form(None)):
    return {"filename": file.filename}
