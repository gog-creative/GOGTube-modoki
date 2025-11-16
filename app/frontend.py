try:  
    from flask import Flask, request, send_file, render_template, redirect, make_response, url_for,session
    from flask_httpauth import HTTPDigestAuth
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    #Googleログイン
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
    from google_auth_oauthlib.flow import Flow
except:
    raise ImportError("Please install requestment.txt modules before run.\nTo install modules, please execute 'python -m pip install -r requestment.txt'.")
import core
from os.path import basename,splitext
from glob import glob
from uuid import uuid4
from os import environ
#永続化DB
import shelve

print("Imported modules successfully")

### バージョン表記
# Alpha / Beta 開発中
# Pre-Release:リリース準備のビルド
# Release:正式リリース

#バックエンドを呼び出し
system = core.yt_modoki2()

#永続化ファイルの読み込み
db = shelve.open("/app/volume/data")
db["total_download"] = db.get("total_download",0)
db["total_access"] = db.get("total_access",0)
db.close()

def add_dlcount():
    with shelve.open("/app/volume/data") as db:
        db["total_download"] += 1
def get_dlcount():
    with shelve.open("/app/volume/data") as db:
        return db["total_download"]
    
def add_totalcount():
    with shelve.open("/app/volume/data") as db:
        db["total_access"] += 1

def get_totalcount():
    with shelve.open("/app/volume/data") as db:
        return db["total_access"]

#Flask等の定義
app = Flask(__name__)
limiter = Limiter(lambda:request.headers.get('cf-connecting-ip') or get_remote_address(), app=app, default_limits=["60 per minute"])
app.secret_key = str(uuid4())
auth = HTTPDigestAuth()

#{google_id:User()}
user_dic:dict[str, "User"] = {}

#Googleユーザーのクラス
class User():
    def __init__(self, id, email=None, name=None, domain=None, is_admin :bool = False):
        self.id = id
        self.email = email
        self.name = name
        self.domain = domain
        self.is_admin = is_admin

    def __eq__(self, value):
        return value.id == self.id
    
    def __str__(self):
        return f"{self.name or self.email or self.id}"

#匿名ユーザーを追加
user_dic["anonymous"] = User("anonymous","-","Anonymous","")

#Googleログイン
if system.config.admin["google_oauth"] == True:
    google_secret = {
                "web":{
                    "client_id":    environ.get("GOOGLE_OAUTH_CLIENT_ID",   ""),
                    "project_id":   environ.get("GOOGLE_OAUTH_PROJECT_ID",  ""),
                    "auth_uri":     environ.get("GOOGLE_OAUTH_AUTH_URI",    "https://accounts.google.com/o/oauth2/auth"),
                    "token_uri":    environ.get("GOOGLE_OAUTH_TOKEN_URI",   "https://oauth2.googleapis.com/token"),
                    "auth_provider_x509_cert_url":
                                    environ.get("GOOGLE_OAUTH_AUTH_PROVIDER_x509_CERT_URL","https://www.googleapis.com/oauth2/v1/certs"),
                    "client_secret":environ.get("GOOGLE_OAUTH_CLIENT_SECRET",""),
                    "redirect_uri": environ.get("GOOGLE_OAUTH_REDIRECT_URI","https://yt.gog-lab.org/google-callback")
                }
            }
    google_flow = Flow.from_client_config(
        google_secret,
        scopes=["openid", "https://www.googleapis.com/auth/userinfo.profile", "https://www.googleapis.com/auth/userinfo.email"],
        redirect_uri = google_secret["web"]["redirect_uri"]
        )

@app.route('/login')
def google_login():
    if system.config.admin["google_oauth"] == False:
        return redirect(url_for('index'))
    authorization_url, state = google_flow.authorization_url()
    session['state'] = state
    return render_template("login.html",link=authorization_url)

@app.route('/google-callback')
def google_callback():
    if system.config.admin["google_oauth"] == False:
        return redirect(url_for('index'))
    
    google_flow.fetch_token(authorization_response = request.url)

    if not session['state'] == request.args['state']:
        print(f"state {session['state']} != {request.args['state']}")
        return 'State mismatch error', 400

    credentials = google_flow.credentials
    request_session = google_requests.Request()

    #credentials.id_tokenが動的属性のため型チェックを無視
    id_info = id_token.verify_oauth2_token(
        credentials.id_token, request_session, google_secret["web"]["client_id"] # type: ignore
    )
    user_dic[id_info.get("sub")] = User(
        id_info.get("sub"),
        id_info.get("email"),
        id_info.get("name"),
        id_info.get("hd")
        )
    #セッションとGoogle_idを紐づけ
    session["id"] = id_info.get("sub")
    return redirect(url_for('index'))


def int_f(n):
    if n == None:
        return "不明"
    try:
        n = int(n)
        return f"{n:,}"
    except (TypeError, ValueError):
        return "0"

app.jinja_env.filters["int_f"] = int_f
app.jinja_env.globals["site_name"] = system.SITE_NAME

#メインページ
@app.route("/")
def index():
    output = {"new_request":system.config.download["new_request"]} 
    add_totalcount()
    return render_template(
        "index.html",
        output=output,
        user_dic=user_dic,
        launch_total = len(system.video_dic.items()),
        total_download = get_dlcount(),
        total_access = get_totalcount(),
        start_time = system.start_time.strftime("%Y年%m月%d日 %H時%M分"),
        version=system.VERSION,
        ytdlp_version=core.yt_dlp.version.__version__,
        config=system.config
        )

#YouTube検索
#4秒程度かかる
@app.route("/search")
@limiter.limit("15 per minute")
def youtube_search():
    query = request.args.get('q')
    if query == None:
        return redirect(url_for("index"))
    entries:list[dict] = system.yt_search(query)
    system.log(f"[{session.get('id','anonymous')}] YouTube検索：{query}")
    return render_template("search_result.html",
                           query=query,
                           entries=entries,
                           
                           )

#ダウンロードのPOST受付
@app.route("/request",methods=["POST"])
@limiter.limit("15 per hour", key_func=lambda:session.get("id","anonymous"))
def download_request():
    output = {}
    
    #Googleログインしていないなら
    if not session.get("id") in user_dic and system.config.admin["google_oauth"] == True:
        return redirect(url_for('google_login'))
    #ログイン不要時：
    elif not session.get("id") in user_dic:
        user_ip = request.headers.get('cf-connecting-ip', get_remote_address())
        user = User(user_ip)
        user_dic[user_ip] = user
        session["id"] = user_ip
    elif session.get("id") in user_dic:
        user = user_dic[session.get("id", request.headers.get('cf-connecting-ip', get_remote_address()))]

    if system.config.download["new_request"] == False:
        return render_template("error.html",
                               output = {"error":"只今新しいリクエストを受け付けておりません。"}),403
    
    #POST以外はエラー
    if request.method != "POST":
        output["error"] = "送信メゾットはPOSTである必要があります"
        return render_template("error.html", output=output), 502

    #パラメーター取得
    type = request.form.get("type","url")
    link = request.form["link"]
    file_type = request.form.get("file_type","video")
    video_codec = request.form.get("video_codec","")
    audio_codec = request.form.get("audio_codec","")
    play_directly = request.form.get("play_directly","off") == "on"
    
    print(video_codec)

    #渡されたのがURLかキーワードか
    match type:
        case "url":
            pass
        case "ytsearch":
            link = "ytsearch:"+link

    #動画か音声かを確認
    if file_type != "video" and file_type != "audio":
        output["error"] = "動画または音声を選択してください"
        return render_template("error.html", output=output),502
    elif file_type == "video":
        is_video = True
    else:
        is_video = False

    uuid, is_exist = system.new_request(link, play_directly, is_video, video_codec, audio_codec, 10, user)
    if not is_exist:
        add_dlcount()

    if play_directly:
        response = make_response(redirect(url_for("streaming",uuid = uuid)))
    else:
        response = make_response(redirect(url_for("status",uuid = uuid)))
    session["video_uuid"] = uuid
    return response

#ステータスのページ(CookieからIDを取得)
@app.route("/status",methods=["GET"])
def cookie_status():
    output = {}
    uuid = session.get("video_uuid", None)
    if not uuid:
        output["error"] = "IDが指定されていません\n先に動画をダウンロードしてください。"
        return render_template("error.html",output = output)
    return redirect(url_for("status",uuid = uuid))

#ステータスのページ
@app.route("/status/<uuid>", methods=["GET"])
def status(uuid):
    output = {}
    status_code = 200
    #ID確認
    try:
        item = system.video_dic[uuid]
    except:
        output["error"] = "そのIDは存在しません"
        return render_template("error.html", output = output,),404
    #渡すデータを準備
    if item.status == "downloading":
        try:
            if item.downloader == None:
                raise IndexError
            progress_info = system.downloader_list[item.downloader].progress_info
        except:
            system.log("ダウンロード進捗取得に失敗しました")
    output["info"] = item.info
    if "entries" in output["info"]:
        output["info"] = output["info"]["entries"][0]
    output["direct_url"] = item.direct_url
    output["status"] = item.status
    
    #待機列の場合
    match item.status:
        case "queue":
            output["message"]=f"待機列：現在{system.queue_list.index(uuid)+1}番目"
            status_code = 202
    #ダウンロード中の場合
        case "downloading":
            status_code = 202
            try:
                output["message"]=f"ダウンロード中...（{item.downloaded_percent} ％完了）"
                output["info"] = progress_info
            except:output["message"]="ダウンロードを開始中..."
    #完了済みの場合
        case "completed":
            output["message"] = "ダウンロード完了"
            status_code = 200
    #エラーの場合
        case "dl_failure":
            output["message"] = "ダウンロードに失敗"
            status_code = 500
    #削除された場合
        case "deleted":
            output["message"] = "ファイルが削除されました"
            status_code = 404
    #動画情報の設定
    output["url"] = f"/download/{uuid}"
    return render_template("status.html",
                           output=output,
                           item=item
                           ),status_code

#ステータスのページ
@app.route("/streaming/<uuid>", methods=["GET"])
def streaming(uuid):
    output = {}
    status_code = 200
    #ID確認
    try:
        item = system.video_dic[uuid]
    except:
        output["error"] = "そのIDは存在しません"
        return render_template("error.html", output = output,),404
    #渡すデータを準備
    if item.status == "downloading":
        try:
            if item.downloader == None:
                raise IndexError
            progress_info = system.downloader_list[item.downloader].progress_info
        except:
            system.log("ダウンロード進捗取得に失敗しました")
    output["info"] = item.info
    if "entries" in output["info"]:
        output["info"] = output["info"]["entries"][0]
    output["direct_url"] = item.direct_url
    output["status"] = item.status
    
    #待機列の場合
    match item.status:
        case "queue":
            output["message"]=f"待機列：現在{system.queue_list.index(uuid)+1}番目"
            status_code = 202
    #完了済みの場合
        case "completed":
            output["message"] = "ダウンロード完了"
            status_code = 200
    #エラーの場合
        case "dl_failure":
            output["message"] = "ダウンロードに失敗"
            status_code = 500
    #削除された場合
        case "deleted":
            output["message"] = "ファイルが削除されました"
            status_code = 404
    #動画情報の設定
    output["url"] = f"/download/{uuid}"
    return render_template("streaming.html",
                           output=output,
                           item=item
                           ),status_code

@app.route("/api/status/<uuid>")
def status_api(uuid):
    #ID確認
    try:
        item = system.video_dic[uuid]
    except:
        return {"error":"そのIDは存在しません"},404
    return {
        "status":item.status,
        "downloaded_percent":item.downloaded_percent,
        "waiting_list_index":system.queue_list.index(uuid) if item.status == "queue" else None,
        "waiting_list_size":len(system.queue_list)
    }

#動画ダウンロード
@app.route("/download/<id>")
def download(id):
    output = {}
    try:
        item = system.video_dic[id]
    except:
        output["error"] = "そのIDは存在しません"
        return render_template("error.html",output=output),404
        
    match item.status:
        case "queue":
            output["error"] = "このファイルはまだダウンロードされていません"
            return render_template("error.html",output=output),404
        case "downloading":
            output["error"] = "このファイルはダウンロード中です"
            return render_template("error.html",output=output),404
        case "dl_failure":
            output["error"] = "ファイルのダウンロードに失敗しました"
            return render_template("error.html",output=output),500
        case "deleted":
            output["error"] = "このファイルは削除されました"
            return render_template("error.html",output=output),404
        
    title = item.info["title"]
    file_name = basename(glob(f"outputs/{id}/output.*")[0])
    file_path = f"outputs/{id}/{file_name}"
    ext = splitext(file_path)[1]
    try:
        return send_file(file_path,
                         max_age=60**2*12,
                         download_name=f"{title}{ext}",
                         as_attachment=True)
    except FileNotFoundError:
        return render_template("error.html",output={"error":"そのファイルは存在しません"})


@auth.get_password
def get_pw(username):
    if username == system.config.admin["login"]:
        return system.config.admin["password"]
    return None

#管理者パネルへリダイレクト
@app.route("/admin")
def admin():
    return redirect(url_for("admin_dashboard"))

#管理者パネル
@app.route("/admin/dashboard",methods=["POST","GET"])
@auth.login_required
def admin_dashboard():
    if request.method=="POST":
        #新規アクセスの可否
        if request.form.get("request_access","off") == "on":
            system.config.download["new_request"] = False
        else:system.config.download["new_request"] = True
        
        #自動削除の設定
        if request.form.get("auto_delete","off") == "on":
            system.config.storage["auto_delete"] = True
        else:system.config.storage["auto_delete"] = False

        #容量制限の設定
        system.config.storage["limit_size"] = float(request.form["limit_size"])
        
        #動画ファイルを全て削除
        if request.form.get("delete_outputs","off") == "on":
            for i in system.video_dic.values():
                try:i.delete()
                except:pass
            system.log("全てのファイルを削除します")

        system.log(f"設定を更新：{system.config}")
        return redirect(url_for("admin"))

    #並び替える
    get_args = request.args.to_dict()
    match get_args.get("sort",None):
        case "size":
            dic = sorted(
                list(system.video_dic.values()),
                key=lambda i:i.file_size,
                reverse=True
                )
        case "older":
            dic = list(system.video_dic.values())
        case "newer"|_:
            dic = list(reversed(system.video_dic.values()))

    #ページを分ける
    page_limit = int(get_args.get("limit","1000"))
    if page_limit < 3:
        page_limit = 3
    
    max_index = (len(dic) + page_limit - 1) // page_limit
    
    page_index = int(get_args.get("page","1")) -1
    # url_forで_external引数を指定しているのは、キーワード引数で意図せず指定してしまうのを防ぐため。
    if page_index < 0:
        page_index = 0
        args = get_args.copy()
        args["page"] = "1"
        return redirect(url_for("admin_dashboard", _external=None, **args))
    
    elif page_index > max_index:
        args = get_args.copy()
        args["page"] = str(max_index + 1)
        return redirect(url_for("admin_dashboard", _external=None, **args))

    dic = dic[page_index*page_limit : (page_index*page_limit) + page_limit]

    page = {
        "index":page_index + 1,
        "max_index":max_index,
        "limit":page_limit,
        "query_args":request.args.to_dict()
    }

    #ステータスも返却
    status = {
        "requests":len(system.video_dic),
        "total_filesize":round(system.total_size(),2),
        "limit_size":system.config.storage["limit_size"]
    }


    return render_template(
        "admin/dashboard.html",
        dic = dic,
        config = system.config,
        status = status,
        page = page,
        add_query_url_for = add_query_url_for,
        )

@app.route("/admin/command",methods=["GET"])
@auth.login_required
def admin_command():
    args = request.args.to_dict()

    #IDとコマンドが指定されている場合
    if args.get("id") and args.get("command"):
        id = args["id"]
        match args["command"]:
            case "log":
                try:
                    return send_file(f"outputs/{id}/log.txt")
                except FileNotFoundError:
                    return render_template("error.html",output={"error":"そのログは見つかりません"}), 404
            case "priority":
                system.log("リクエストを優先に設定します",id)
                system.queue_list.insert(0, system.queue_list.pop(system.queue_list.index(id)))    
                #URIパラメーターから削除
                del args["command"]
                del args["id"]
                return redirect(url_for("admin_dashboard", _external=None, **args))
            
            case "delete":
                system.log("ファイルを削除します", id)
                try:system.video_dic[id].delete()
                except Exception as e:
                    return render_template("error.html",output={"error":e}),500
                #URIパラメーターから削除
                del args["command"]
                del args["id"]
                return redirect(url_for("admin_dashboard", _external=None, **args))

    #コマンドのみの場合
    if args.get("command"):
        match args["command"]:
            case "delete_all":
                system.log("全てのファイルを削除します")
                for i in system.video_dic.values():
                    try:i.delete()
                    except:pass
                return redirect(url_for("admin_dashboard"))
            case "log":
                return send_file("log.txt")
    return redirect(url_for("admin_dashboard"))

@app.route("/profile/<id>")
@auth.login_required
def admin_userpage(id:str):
    #ユーザー
    user = user_dic.get("id",id)
    #そのユーザーによる動画の辞書
    user_video_list=[]
    for uuid, item in system.video_dic.items():
        if item.user == user:
            user_video_list.append(item)
    return render_template(
        "admin/user.html",
        user=user,
        dic=user_video_list,
        config = system.config,
        
        )

#エラーページ
@app.errorhandler(500)
@app.errorhandler(502)
def server_error(error):
    system.log(error,level="ERROR")
    return render_template("error.html",output={"error":"サーバーでの処理中にエラーが発生しました。"}),500

#Bad Request
@app.errorhandler(400)
def bad_request(error):
    system.log(error,level="ERROR")
    return render_template("error.html",output={"error":"送信されたデータ形式が間違っています。"}),400

#Unauthorized
@app.errorhandler(401)
def unauthorized(error):
    system.log(error,level="ERROR")
    return render_template("error.html",output={"error":"このページにアクセスする権限がありません。"}),401

#Not Found
@app.errorhandler(404)
def not_found(error):
    return render_template("error.html",output={"error":"そのページは見つかりませんでした。"}),404

#Too Many Requests
@app.errorhandler(429)
def many_requests(error):
    return render_template("error.html",output={"error":"要求されたリクエストが多すぎます。しばらく経過後、再度お試しください。"}),429

#template内でクエリを追加するurl_forを使うための関数
def add_query_url_for(endpoint,dic,**kwarg):
    _dic = dic.copy()
    for k,v in kwarg.items():
        _dic[k] = v
    return url_for(endpoint,**_dic)

#Pythonファイルを直接実行した場合
if __name__ == "__main__":
    app.run(debug=True,port=80)
    system.log("起動に成功しました（直接実行）")

#WSGIサーバーから実行した場合
else:
    system.log("起動に成功しました（WSGIサーバー経由）")