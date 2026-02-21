from fastapi import FastAPI, APIRouter, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict
import uuid
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
import aiofiles
import shutil
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from cryptography.fernet import Fernet
import base64
import httpx

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Media storage directory
MEDIA_DIR = ROOT_DIR / 'media'
MEDIA_DIR.mkdir(exist_ok=True)

# Token encryption
encryption_key = os.getenv('TOKEN_ENCRYPTION_KEY')
if not encryption_key:
    encryption_key = Fernet.generate_key().decode()
    with open(ROOT_DIR / '.env', 'a') as f:
        f.write(f"\nTOKEN_ENCRYPTION_KEY={encryption_key}")

cipher_suite = Fernet(encryption_key.encode())

# Scheduler
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.start()
    logging.info("Scheduler started")
    yield
    # Shutdown
    scheduler.shutdown()
    client.close()
    logging.info("Scheduler stopped")

# Create the main app
app = FastAPI(lifespan=lifespan)
api_router = APIRouter(prefix="/api")

# Models
class OAuthConnection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    platform: str
    user_id: str
    access_token_encrypted: str
    page_id: Optional[str] = None
    page_name: Optional[str] = None
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True

class OAuthConnectionCreate(BaseModel):
    platform: str
    user_id: str
    access_token: str
    page_id: Optional[str] = None
    page_name: Optional[str] = None

class Template(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    caption: str
    hashtags: List[str] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class TemplateCreate(BaseModel):
    name: str
    caption: str
    hashtags: List[str] = []

class Post(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    media_type: str
    media_url: str
    thumbnail_url: Optional[str] = None
    caption: str
    hashtags: List[str] = []
    platforms: List[str] = []
    scheduled_for: Optional[datetime] = None
    is_recurring: bool = False
    cron_expression: Optional[str] = None
    status: str = "pending"
    platform_post_ids: Dict[str, str] = {}
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    posted_at: Optional[datetime] = None
    error_message: Optional[str] = None

class PostCreate(BaseModel):
    media_type: str
    media_url: str
    thumbnail_url: Optional[str] = None
    caption: str
    hashtags: List[str] = []
    platforms: List[str] = []
    scheduled_for: Optional[str] = None
    is_recurring: bool = False
    cron_expression: Optional[str] = None

class PlatformStats(BaseModel):
    total_posts: int = 0
    successful_posts: int = 0
    failed_posts: int = 0
    pending_posts: int = 0

# Helper Functions
def encrypt_token(token: str) -> str:
    return cipher_suite.encrypt(token.encode()).decode()

def decrypt_token(encrypted_token: str) -> str:
    return cipher_suite.decrypt(encrypted_token.encode()).decode()

async def get_platform_connection(platform: str) -> Optional[dict]:
    connection = await db.oauth_connections.find_one(
        {"platform": platform, "is_active": True},
        {"_id": 0}
    )
    if connection and connection.get('access_token_encrypted'):
        connection['access_token'] = decrypt_token(connection['access_token_encrypted'])
    return connection

async def post_to_instagram(connection: dict, media_url: str, caption: str, media_type: str):
    """Post to Instagram using Graph API"""
    access_token = connection['access_token']
    user_id = connection['user_id']
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create media container
        container_data = {
            "access_token": access_token,
            "caption": caption
        }
        
        if media_type == "image":
            container_data["image_url"] = media_url
        else:
            container_data["media_type"] = "VIDEO"
            container_data["video_url"] = media_url
        
        response = await client.post(
            f"https://graph.instagram.com/v20.0/{user_id}/media",
            data=container_data
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Instagram upload failed: {response.text}")
        
        media_id = response.json().get('id')
        
        # Publish media
        publish_response = await client.post(
            f"https://graph.instagram.com/v20.0/{user_id}/media_publish",
            data={
                "creation_id": media_id,
                "access_token": access_token
            }
        )
        
        if publish_response.status_code != 200:
            raise HTTPException(status_code=publish_response.status_code, detail=f"Instagram publish failed: {publish_response.text}")
        
        return publish_response.json().get('id')

async def post_to_facebook(connection: dict, media_url: str, caption: str, media_type: str):
    """Post to Facebook using Graph API"""
    access_token = connection['access_token']
    page_id = connection.get('page_id', connection['user_id'])
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        if media_type == "image":
            response = await client.post(
                f"https://graph.facebook.com/v20.0/{page_id}/photos",
                data={
                    "url": media_url,
                    "caption": caption,
                    "access_token": access_token
                }
            )
        else:
            response = await client.post(
                f"https://graph.facebook.com/v20.0/{page_id}/videos",
                data={
                    "file_url": media_url,
                    "description": caption,
                    "access_token": access_token
                }
            )
        
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Facebook post failed: {response.text}")
        
        return response.json().get('id')

async def post_to_youtube(connection: dict, video_url: str, caption: str, thumbnail_url: Optional[str] = None):
    """Post to YouTube using Data API"""
    # This is a simplified version - actual implementation requires google-api-python-client
    # For now, return a mock response
    return f"youtube_{uuid.uuid4()}"

async def execute_post(post_id: str):
    """Execute posting to all selected platforms"""
    post = await db.posts.find_one({"id": post_id}, {"_id": 0})
    if not post:
        logging.error(f"Post {post_id} not found")
        return
    
    platform_post_ids = {}
    errors = []
    
    for platform in post['platforms']:
        try:
            connection = await get_platform_connection(platform)
            if not connection:
                errors.append(f"{platform}: Not connected")
                continue
            
            full_caption = f"{post['caption']}\n\n{' '.join(post.get('hashtags', []))}"
            
            if platform == "instagram":
                post_id_result = await post_to_instagram(
                    connection,
                    post['media_url'],
                    full_caption,
                    post['media_type']
                )
                platform_post_ids['instagram'] = post_id_result
            elif platform == "facebook":
                post_id_result = await post_to_facebook(
                    connection,
                    post['media_url'],
                    full_caption,
                    post['media_type']
                )
                platform_post_ids['facebook'] = post_id_result
            elif platform == "youtube":
                if post['media_type'] == "video":
                    post_id_result = await post_to_youtube(
                        connection,
                        post['media_url'],
                        full_caption,
                        post.get('thumbnail_url')
                    )
                    platform_post_ids['youtube'] = post_id_result
                else:
                    errors.append(f"youtube: Only videos supported")
        except Exception as e:
            logging.error(f"Error posting to {platform}: {str(e)}")
            errors.append(f"{platform}: {str(e)}")
    
    # Update post status
    status = "completed" if platform_post_ids and not errors else "failed" if errors else "completed"
    await db.posts.update_one(
        {"id": post_id},
        {
            "$set": {
                "status": status,
                "platform_post_ids": platform_post_ids,
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "error_message": "; ".join(errors) if errors else None
            }
        }
    )

# OAuth Endpoints
@api_router.post("/oauth/connect", response_model=OAuthConnection)
async def connect_platform(input: OAuthConnectionCreate):
    encrypted_token = encrypt_token(input.access_token)
    
    connection = OAuthConnection(
        platform=input.platform,
        user_id=input.user_id,
        access_token_encrypted=encrypted_token,
        page_id=input.page_id,
        page_name=input.page_name
    )
    
    # Deactivate old connections for this platform
    await db.oauth_connections.update_many(
        {"platform": input.platform},
        {"$set": {"is_active": False}}
    )
    
    doc = connection.model_dump()
    doc['connected_at'] = doc['connected_at'].isoformat()
    await db.oauth_connections.insert_one(doc)
    
    return connection

@api_router.get("/oauth/connections")
async def get_connections():
    connections = await db.oauth_connections.find(
        {"is_active": True},
        {"_id": 0, "access_token_encrypted": 0}
    ).to_list(100)
    
    for conn in connections:
        if isinstance(conn.get('connected_at'), str):
            conn['connected_at'] = datetime.fromisoformat(conn['connected_at'])
    
    return connections

@api_router.delete("/oauth/disconnect/{platform}")
async def disconnect_platform(platform: str):
    await db.oauth_connections.update_many(
        {"platform": platform},
        {"$set": {"is_active": False}}
    )
    return {"message": f"{platform} disconnected"}

# Template Endpoints
@api_router.post("/templates", response_model=Template)
async def create_template(input: TemplateCreate):
    template = Template(**input.model_dump())
    doc = template.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    await db.templates.insert_one(doc)
    return template

@api_router.get("/templates", response_model=List[Template])
async def get_templates():
    templates = await db.templates.find({}, {"_id": 0}).to_list(1000)
    for template in templates:
        if isinstance(template.get('created_at'), str):
            template['created_at'] = datetime.fromisoformat(template['created_at'])
    return templates

@api_router.get("/templates/{template_id}", response_model=Template)
async def get_template(template_id: str):
    template = await db.templates.find_one({"id": template_id}, {"_id": 0})
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if isinstance(template.get('created_at'), str):
        template['created_at'] = datetime.fromisoformat(template['created_at'])
    return template

@api_router.put("/templates/{template_id}", response_model=Template)
async def update_template(template_id: str, input: TemplateCreate):
    result = await db.templates.update_one(
        {"id": template_id},
        {"$set": input.model_dump()}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    
    template = await db.templates.find_one({"id": template_id}, {"_id": 0})
    if isinstance(template.get('created_at'), str):
        template['created_at'] = datetime.fromisoformat(template['created_at'])
    return template

@api_router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    result = await db.templates.delete_one({"id": template_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"message": "Template deleted"}

# Media Upload
@api_router.post("/upload")
async def upload_media(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    file_ext = Path(file.filename).suffix
    file_path = MEDIA_DIR / f"{file_id}{file_ext}"
    
    async with aiofiles.open(file_path, 'wb') as f:
        content = await file.read()
        await f.write(content)
    
    # Determine media type
    media_type = "image" if file_ext.lower() in ['.jpg', '.jpeg', '.png', '.gif'] else "video"
    
    # Return URL (in production, this would be a CDN URL)
    backend_url = os.getenv('REACT_APP_BACKEND_URL', 'http://localhost:8001')
    media_url = f"{backend_url}/api/media/{file_id}{file_ext}"
    
    return {
        "media_url": media_url,
        "media_type": media_type,
        "filename": file.filename
    }

@api_router.get("/media/{filename}")
async def get_media(filename: str):
    from fastapi.responses import FileResponse
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

# Post Endpoints
@api_router.post("/posts", response_model=Post)
async def create_post(input: PostCreate, background_tasks: BackgroundTasks):
    post = Post(**input.model_dump())
    
    # Parse scheduled_for if provided
    if input.scheduled_for:
        post.scheduled_for = datetime.fromisoformat(input.scheduled_for.replace('Z', '+00:00'))
    
    doc = post.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    if doc.get('scheduled_for'):
        doc['scheduled_for'] = doc['scheduled_for'].isoformat()
    
    await db.posts.insert_one(doc)
    
    # Schedule the post
    if post.is_recurring and post.cron_expression:
        # Recurring post
        scheduler.add_job(
            execute_post,
            CronTrigger.from_crontab(post.cron_expression),
            args=[post.id],
            id=f"recurring_{post.id}",
            replace_existing=True
        )
    elif post.scheduled_for:
        # One-time scheduled post
        scheduler.add_job(
            execute_post,
            DateTrigger(run_date=post.scheduled_for),
            args=[post.id],
            id=f"scheduled_{post.id}",
            replace_existing=True
        )
    else:
        # Post immediately
        background_tasks.add_task(execute_post, post.id)
    
    return post

@api_router.get("/posts", response_model=List[Post])
async def get_posts(status: Optional[str] = None, limit: int = 100):
    query = {}
    if status:
        query['status'] = status
    
    posts = await db.posts.find(query, {"_id": 0}).sort("created_at", -1).to_list(limit)
    
    for post in posts:
        if isinstance(post.get('created_at'), str):
            post['created_at'] = datetime.fromisoformat(post['created_at'])
        if post.get('scheduled_for') and isinstance(post['scheduled_for'], str):
            post['scheduled_for'] = datetime.fromisoformat(post['scheduled_for'])
        if post.get('posted_at') and isinstance(post['posted_at'], str):
            post['posted_at'] = datetime.fromisoformat(post['posted_at'])
    
    return posts

@api_router.get("/posts/{post_id}", response_model=Post)
async def get_post(post_id: str):
    post = await db.posts.find_one({"id": post_id}, {"_id": 0})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    if isinstance(post.get('created_at'), str):
        post['created_at'] = datetime.fromisoformat(post['created_at'])
    if post.get('scheduled_for') and isinstance(post['scheduled_for'], str):
        post['scheduled_for'] = datetime.fromisoformat(post['scheduled_for'])
    if post.get('posted_at') and isinstance(post['posted_at'], str):
        post['posted_at'] = datetime.fromisoformat(post['posted_at'])
    
    return post

@api_router.delete("/posts/{post_id}")
async def delete_post(post_id: str):
    # Remove from scheduler if exists
    try:
        scheduler.remove_job(f"scheduled_{post_id}")
    except:
        pass
    try:
        scheduler.remove_job(f"recurring_{post_id}")
    except:
        pass
    
    result = await db.posts.delete_one({"id": post_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"message": "Post deleted"}

# Stats
@api_router.get("/stats")
async def get_stats():
    total_posts = await db.posts.count_documents({})
    completed = await db.posts.count_documents({"status": "completed"})
    failed = await db.posts.count_documents({"status": "failed"})
    pending = await db.posts.count_documents({"status": "pending"})
    
    platforms = {}
    for platform in ["instagram", "facebook", "youtube"]:
        platforms[platform] = {
            "total_posts": await db.posts.count_documents({"platforms": platform}),
            "successful_posts": await db.posts.count_documents({
                "platforms": platform,
                "status": "completed"
            }),
            "failed_posts": await db.posts.count_documents({
                "platforms": platform,
                "status": "failed"
            }),
            "pending_posts": await db.posts.count_documents({
                "platforms": platform,
                "status": "pending"
            })
        }
    
    connections = await db.oauth_connections.find(
        {"is_active": True},
        {"_id": 0, "platform": 1}
    ).to_list(100)
    
    connected_platforms = [c['platform'] for c in connections]
    
    return {
        "total_posts": total_posts,
        "completed_posts": completed,
        "failed_posts": failed,
        "pending_posts": pending,
        "platform_stats": platforms,
        "connected_platforms": connected_platforms
    }

# Health check
@api_router.get("/")
async def root():
    return {"message": "Social Media Automation API"}

# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
