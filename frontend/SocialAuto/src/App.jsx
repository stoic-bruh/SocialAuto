import { useEffect, useState } from "react";

export default function App() {
  const [templates, setTemplates] = useState([]);
  const [file, setFile] = useState(null);
  const [caption, setCaption] = useState("");
  const [uploadResult, setUploadResult] = useState(null);

  useEffect(() => {
    fetch("http://localhost:8001/api/templates")
      .then(res => res.json())
      .then(data => setTemplates(data));
  }, []);

  const uploadMedia = async () => {
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("http://localhost:8001/api/upload", {
      method: "POST",
      body: formData
    });

    const data = await res.json();
    setUploadResult(data);
  };

  const createPost = async () => {
    const post = {
      media_type: uploadResult.media_type,
      media_url: uploadResult.media_url,
      caption: caption,
      hashtags: [],
      platforms: ["instagram"],
      is_recurring: false
    };

    await fetch("http://localhost:8001/api/posts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(post)
    });

    alert("Post created! (not published yet)");
  };

  return (
    <div style={{ padding: 20 }}>
      <h1>SocialAuto Dashboard</h1>

      <h2>Upload Media</h2>
      <input type="file" onChange={e => setFile(e.target.files[0])} />
      <br /><br />
      <button onClick={uploadMedia}>Upload</button>

      {uploadResult && (
        <>
          <p>Uploaded: {uploadResult.filename}</p>
          <img src={uploadResult.media_url} width="250" />
        </>
      )}

      <h2>Create Post</h2>
      <textarea
        rows="4"
        cols="50"
        placeholder="Write caption..."
        value={caption}
        onChange={e => setCaption(e.target.value)}
      />
      <br /><br />
      <button onClick={createPost} disabled={!uploadResult}>
        Create Post
      </button>

      <hr />

      <h2>Templates</h2>
      {templates.map(t => (
        <div key={t.id} style={{border:"1px solid #ccc", margin:10, padding:10}}>
          <h3>{t.name}</h3>
          <p>{t.caption}</p>
          <small>{t.hashtags.join(" ")}</small>
        </div>
      ))}
    </div>
  );
}