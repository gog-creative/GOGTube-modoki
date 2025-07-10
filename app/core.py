#フロントエンドと分離する際のバックエンドスクリプトです

"""
CREATED BY GOG-Creaive

YT-dlpラッパーのバックエンドスクリプト。
動画のキュー管理からダウンロードまで、これ一つで完結します。

機能
・設定の読み込み/保存
・リクエストのキュー、バックグラウンドでの動画ダウンロード
・ストレージの管理機能
・直接管理機能

"""

import threading
from time import sleep
from typing import Optional, Any, Literal
import uuid
from glob import glob
from shutil import rmtree
from os import remove, mkdir, environ
from os.path import getsize
import datetime

from yt_dlp import YoutubeDL
import yt_dlp
import yt_dlp.version

class yt_modoki2:
    SITE_NAME = "GOGTubeもどき"
    VERSION = "Release 4.0.1 build-20250709"
    
    #設定保管オブジェクト
    class settings:
        #JSONファイルを読み込み
        def __init__(self, super:"yt_modoki2"):
            self.dic = {
                "ADMIN":{
                    "login":str(environ.get("ADMIN_LOGIN","admin")),
                    "password":str(environ.get("ADMIN_PASSWORD","password")),
                    "google_oauth":environ.get("ADMIN_GOOGLE_OAUTH", "").lower() == "true",
                    "debug":environ.get("ADMIN_DEBUG","").lower() == "true"
                },
                "DOWNLOAD":{
                    "bind_ip":environ.get("DOWNLOAD_BIND_IP","0.0.0.0"),
                    "threads":int(environ.get("DOWNLOAD_THREADS","2")),
                    "new_request":environ.get("ADMIN_NEW_REQUEST","").lower() == "true",
                    "pot_provider":environ.get("POT_PROVIDER","http://ytmp3modoki2-bgutil-provider-1:4416")
                },
                "STORAGE":{
                    "time_format":"%Y/%m/%d %H:%M:%S",
                    "limit_size":float(environ.get("STORAGE_LIMIT_SIZE","3000")),
                    "auto_delete":bool(environ.get("STORAGE_AUTO_DELETE","True"))
                }
                }

            self._core: yt_modoki2 = super
            self.admin = self.dic["ADMIN"]
            self.download = self.dic["DOWNLOAD"]
            self.storage = self.dic["STORAGE"]

        def __str__(self):
            return f"\nADMIN {self.admin}\nDOWNLOAD {self.download}\nSTORAGE {self.storage}\n"

    #動画オブジェクト
    class video_item:
        #呼び出し時に変数を定義
        def __init__(self,super:"yt_modoki2", uuid:str, url:str, play_directly:bool, is_video:bool, video_codec:str, audio_codec:str, save_period, user):
            self._core: yt_modoki2 = super
            self.uuid:str = uuid                    #リクエストアイテムを識別するためのUUID
            self.url:str = url                      #ダウンロードするURL
            self.play_directly:bool = play_directly #直接再生するかどうか
            self.is_video:bool = is_video           #動画か音声か
            self.status:Literal["queue","downloading","completed","dl_failure","deleted"] = "queue" #動画のステータス
            self.downloaded_percent:float = 0.0     #ダウンロード進捗
            self.file_size:float = 0.0              #ファイルサイズ（MB）
            self.audio_codec:str = audio_codec      #音声コーデック
            self.video_codec:str = video_codec      #動画コーデック
            self.downloader :Optional[int] = None   #ダウンローダーID
            self.user:Any = user                    #ユーザー識別
            self.ytdlp_format:Optional[str] = None  #YT-dlpに渡すフォーマットの絞り込み
            self.direct_url:Optional[str] = None    #直接再生するURL
            
            #動画の情報
            self.info:dict[str,Any] = {}

            #時間情報
            self.time = {
                "request":datetime.datetime.now(),
                "start_download":"-",
                "finish_download":"-",
                "finish":"-",
                "min_save_period":save_period, #最低限保存する時間（分）
                "save_period":None  #保存する期限時刻（datatime）w
            }

        #動画ファイルを削除する関数
        def delete(self,by:str= "") -> None:
            by=by+": "
            try:
                if self.play_directly == True:
                    self._core.log(f"直接再生なので削除はスキップします", self.uuid)
                    return
                elif self.status == "completed":
                    file_name = glob(f"outputs/{self.uuid}/output.*")[0]
                    remove(file_name)
                    self.file_size = 0
                    self.status = "deleted"
                    self._core.log(f"{by}ファイル {file_name}を削除しました", self.uuid)
                else:
                    raise Exception("この動画は現在保存されていません")
            except IndexError:
                self._core.log(f"{by}ファイルの削除に失敗しました: 既に存在しません",self.uuid,"ERROR")
                raise IndexError(f"{by}ファイルの削除に失敗しました: 既に存在しません")
            except FileNotFoundError:
                self._core.log(f"{by}ファイル {file_name}の削除に失敗しました: 既に存在しません",self.uuid,"ERROR")
                raise FileNotFoundError(f"{by}ファイル {file_name}の削除に失敗しました: 既に存在しません")
            except PermissionError:
                self._core.log(f"{by}ファイル {file_name}の削除に失敗しました: 現在アクセスされている、または削除権限がありません",self.uuid,"INFO")
                raise PermissionError(f"{by}ファイル {file_name}の削除に失敗しました: 現在アクセスされている、または削除権限がありません")
            except KeyError:
                self._core.log(f"{by}ファイル {file_name}を削除しました", self.uuid)
                raise KeyError(f"{by}ファイル {file_name}を削除しました")
            
            except Exception as error:
                self._core.log(f"{by}ファイルの削除に失敗しました: {error.__class__.__name__}: {error}", self.uuid,"ERROR")
                raise Exception(f"{by}ファイルの削除に失敗しました: {error.__class__.__name__}: {error}")
        
        

        def priority(self):
            #ステータスがキューではない場合
            if not self.status == "queue":
                raise Exception("この動画はキューにありません")
            #UUIDがキューにない場合
            if not self.uuid in self._core.queue_list:
                raise Exception("この動画はキューにありません。")
            
            #キューの先頭に移動
            self._core.queue_list.remove(self.uuid)
            self._core.queue_list.insert(0,self.uuid)
            self._core.log("リクエストをキューの先頭に移動しました",self.uuid)

        def posteriority(self):
            #ステータスがキューではない場合
            if not self.status == "queue":
                raise Exception("この動画はキューにありません")
            #UUIDがキューにない場合
            if not self.uuid in self._core.queue_list:
                raise Exception("この動画はキューにありません。")
            
            #キューの末尾に移動
            self._core.queue_list.remove(self.uuid)
            self._core.queue_list.append(self.uuid)
            self._core.log("リクエストをキューの末尾に移動しました",self.uuid)
        
        def __str__(self):
            text = f"[{self.user}]によるリクエスト {self.uuid}:\n"+\
            f"    URL:{self.url}"
            f"    日時:{self.time["request"]}"
            return text
    
    #ダウンロードスレッドのオブジェクト
    class downloader:
        def __init__(self,super:"yt_modoki2"):
            self._core: yt_modoki2 = super
            self.progress_info: Optional[dict] = None
            self.id: Optional[str] = None
            self.item: yt_modoki2.video_item

        #YT-DLP処理
        def ytdlp_download(self,thread_id:int):
            #ループ
            while True:
                # 処理なしの場合
                if self._core.queue_list==[]:
                    sleep(0.5)
                    continue
                
                
                # キューから取得、削除
                self.id = self._core.queue_list.pop(0)
                self.item = self._core.video_dic[self.id]
                self._core.log(f"処理開始",self.id)
                self.progress_info = {}
                
                # ダウンローダーIDを設定
                self.item.downloader = thread_id
                
                #直接ダウンロードの場合
                if self.item.play_directly:
                    self.item.status = "downloading"
                    try:
                        if self.item.is_video:
                            self.item.ytdlp_format="best"
                            self.item.info = YoutubeDL(
                                {
                                    "format":self.item.ytdlp_format,
                                    "extractor_args": {
                                        "youtubepot-bgutilhttp": {"base_url":self._core.config.download["pot_provider"]}
                                    },
                                    "noplaylist":True
                                    }
                                ).extract_info(self.item.url, False) or {}
                        else:
                            self.item.ytdlp_format = "ba[ext='m4a']/ba[acodec='mp3']/ba"
                            self.item.info = YoutubeDL(
                                {
                                    "format":self.item.ytdlp_format,
                                    "noplaylist":True,
                                    "extractor_args": {
                                        "youtubepot-bgutilhttp": {"base_url":self._core.config.download["pot_provider"]}
                                    },
                                }).extract_info(self.item.url, False) or {}
                    except Exception:
                        self.item.status = "dl_failure"
                        continue

                    #URLを抽出
                    if "entries" in self.item.info:
                        formats = self.item.info["entries"][0]["formats"]
                        target_format_id = self.item.info["entries"][0].get("format_id")
                    else:
                        formats = self.item.info["formats"]
                        target_format_id = self.item.info.get("format_id")

                    target_url = None

                    for i in formats:
                        if i["format_id"] == target_format_id:
                            target_url = i["url"]
                            break

                    self.item.direct_url = target_url
                    
                    #完了
                    self._core.log("処理が完了しました", str(self.item.uuid))
                    self.item.time["finish"] = datetime.datetime.now()
                    self.item.status = "completed"
                    continue

                # ステータス更新
                self.item.status = "downloading"
                
                # YT-dlp オプション設定
                ytdlp_option = {
                    "outtmpl":f"outputs/{self.id}/output.%(ext)s",
                    "noplaylist":True,
                    "noprogress": True,
                    "writethumbnail":True,
                    "verbose":self._core.config.admin["debug"],
                    "debug_printtraffic":self._core.config.admin["debug"],
                    "source_address":self._core.config.download["bind_ip"],
                    #POトークン発行
                    "extractor_args": {
                        "youtubepot-bgutilhttp": {"base_url":self._core.config.download["pot_provider"]}
                    },
                    #"throttled_rate":"50K",
                    #"retries":"3",
                    "progress_hooks":[self.progress_hook]
                    }
                
                ytdlp_option["merge_output_format"]="mp4"

                #動画/音声を判定しフォーマットを選択
                #動画の場合
                if self.item.is_video:
                    if self.item.audio_codec != "":
                        audio = f"ba[acodec='{self.item.audio_codec}']/ba[acodec='mp3']/ba[acodec='opus']/ba"
                    match self.item.video_codec:
                        case "latest":
                            video="bv[ext=mp4]/bv/best"
                            audio="ba"

                        case "ios" | _:
                            video="bv[vcodec!~='^(vp0?9|av0?1|h265)']/bv/best"
                            audio="ba[ext='m4a']/ba[acodec='mp3']/ba"

                    ytdlp_option["format"]=f"({video})+({audio})"
                #音声だけの場合
                else:
                    if self.item.audio_codec != "":
                        ytdlp_option["format"] = f"ba[acodec='{self.item.audio_codec}']/ba[acodec='aac']/ba"
                    else:
                        ytdlp_option["format"] = "ba[ext='m4a']/ba[ext='mp3']/ba"
                
                #フォーマットを保存
                self.item.ytdlp_format = ytdlp_option["format"]
                print(self.item.ytdlp_format)
                # ダウンロード
                self._core.log(f"ダウンロードを開始\nフォーマット：{ytdlp_option["format"]}",self.id)
                self.item.time["start_download"] = datetime.datetime.now()

                try:
                    self.item.info = YoutubeDL(ytdlp_option).extract_info(self.item.url) or {}
                    self.item.time["finish_download"] = datetime.datetime.now()
                    if bool(glob(f"outputs/{self.id}/output.*")) == False:
                        raise FileNotFoundError
                
                except Exception as error:
                    self._core.log("ダウンロードに失敗しました: "+f"{error.__class__.__name__}: {error}" ,self.id,"ERROR")
                    self.item.time["finish"] = datetime.datetime.now()
                    self.item.status = "dl_failure"
                    continue
                
                if "upload_date" in self.item.info :
                    self.item.info["upload_date"] = datetime.datetime.strptime(self.item.info.get("upload_date","20000101"),"%Y%m%d")

                #ダウンロード完了
                            #サイズ、時刻等の設定
                self.item.file_size = round(getsize(glob(f"outputs/{self.item.uuid}/output.*")[0])/1024**2,2)
                self.item.time["save_period"] = (datetime.datetime.now() + datetime.timedelta(minutes=self.item.time["min_save_period"]))

                #完了
                self.item._core.log("処理が完了しました",str(self.item.uuid))
                self.item.time["finish"] = datetime.datetime.now()
                self.item.status = "completed"

        #YT-dlpの進捗フック
        def progress_hook(self, d:dict) -> None:
            self.progress_info = d
            if "downloaded_bytes" in self.progress_info and "total_bytes" in self.progress_info:
                self.item.downloaded_percent = float(round(self.progress_info["downloaded_bytes"] / self.progress_info["total_bytes"]*100,2))
    
    def __init__(self):
        print(f"YT-modoki2 {__name__}を起動します",flush=True)
        print(f"yt-dlp version: {yt_dlp.version.__version__}",flush=True)
        #リクエストデータ（UUID:self.video_item）
        self.video_dic: dict[str, yt_modoki2.video_item] = {}
        #ダウンロードキュー
        self.queue_list: list[str] = []
        #ダウンローダーのスレッドリスト
        self.downloader_list:list[yt_modoki2.downloader] = []
        self.config = self.settings(self)

        #起動時刻の記録
        self.start_time = datetime.datetime.now()

        #ダウンロードスレッドを開始
        for i in range(self.config.download["threads"]):
            self.downloader_list.append(self.downloader(self))
            threading.Thread(target=self.downloader_list[i].ytdlp_download,args=(i,)).start()
        
        #自動削除スレッドを開始
        threading.Thread(target=self._auto_delete).start()
        
        #outputsフォルダ内を削除
        try:
            rmtree("outputs") 
        except:
            pass
        mkdir("outputs")
        

    #新しいリクエストを受け取ったとき
    def new_request(
            self,
            url :str,
            play_directly :bool = False,
            is_video :bool = True,
            video_codec :str = "",
            audio_codec :str = "",
            min_save_period :float = 10.0,
            user:Any = None) -> tuple[str, bool]:
        
        """動画/音声をダウンロードするリクエストをキューに追加します。
            Args:
                url (str): 動画、音声のURL（必須）
                is_video (bool, optional): 動画をダウンロードするかどうか。Flaseだと音声のみ。デフォルトではTrue
                video_codec (str, optional): 優先してダウンロードする動画のコーデック。
                audio_codec (str, optional): 優先してダウンロードする音声のコーデック。
                min_save_period (float, optional): 自動削除されない最低期間（分）。デフォルトでは10分。
                user (Any, optional): 使用者識別
            Returns:
                str: リクエスト固有のUUID（リクエストの内容が同じなら重複）
        """
        #ユーザー（IP）、日付、リクエスト内容が同じならUUIDが重複する
        self._uuid = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                str(
                    {
                        "url":url,
                        "play_directly":play_directly,
                        "is_video":is_video,
                        "ip":user,
                        "video_codec":video_codec,
                        "audio_codec":audio_codec,
                        "requested_date":datetime.datetime.now().strftime("%Y/%m/%d")
                        }
                )
            )
        )
        #もし重複していたらそのままUUIDを返す
        if self._uuid in self.video_dic:
            return self._uuid, True
        
        #リクエストを追加
        self.video_dic[self._uuid] = self.video_item(self, self._uuid, url, play_directly, is_video, video_codec, audio_codec, min_save_period, user)
        try:
            mkdir(f"outputs/{self._uuid}")
            self.log(f"'{user}'から新しいリクエストを受け取りました: {url}",self._uuid)
            self.queue_list.append(self._uuid)
        except OSError as e:
            self.video_dic[self._uuid].status = "dl_failure"
        return self._uuid, False
    
    def yt_search(self, query:str) -> list[dict]:
        info = YoutubeDL().extract_info(f"ytsearch38:{query}",download=False,process=False) or {"entries":[]}
        return list(info["entries"])

    #動画ファイルの合計サイズを計算
    def total_size(self) -> float:
        self.total:float = 0.0
        for i in self.video_dic.values():
            self.total += i.file_size
        return self.total
    
    #指定要領に達したら自動削除
    def _auto_delete(self):
        while True:
            sleep(10)
            
            #自動削除無効ならスキップ
            if self.config.storage["auto_delete"] == False:
                continue
            #条件を満たしていないならスキップ
            if self.config.storage["limit_size"] >= self.total_size():
                continue
            
            self.count=0
            for i in self.video_dic.values():
                #既にダウンロードされていないならスキップ
                if i.status != "completed":
                    continue
                #保存期限内ならスキップ
                if i.time["save_period"] > datetime.datetime.now():
                    continue
                #削除試行、失敗したら次へ
                try:
                    i.delete("自動削除")
                    self.count += 1
                except:
                    continue
                #下回ったら終了
                if self.config.storage["limit_size"] > self.total_size():
                    break

            self.log(f"自動削除: {self.count}個のファイルを削除しました")
        
    #ログ保存関数
    def log(self,text:str,uuid=None,level:str="INFO") -> None:   
        #時刻とメッセージを定義
        time = datetime.datetime.now().strftime(self.config.storage["time_format"])
        message = f"{time} [{level}] [{uuid}] {text}"

        #特定リクエスト内でのログの場合
        if uuid:
            with open(f"outputs/{uuid}/log.txt", mode="a+", encoding="utf-8") as l:
                message = f"{time} [{level}] {text}"
                l.write("\n" + message)
                print(message)
        else:
            print(message)

        #ログ保存
        with open("log.txt", mode="a+", encoding="utf-8") as l:
            message = f"{time} [{level}] [{uuid}] {text}"
            l.write("\n"+message)

if __name__ == "__main__":
    print("このファイルは直接実行出来ません。代わりにfrontend.pyを実行するか、独自のラッパーを開発してください。")
    print("詳細はhttps://github.com/gog-creative/youtube-modoki-2 を参照してください。")
    exit(1)