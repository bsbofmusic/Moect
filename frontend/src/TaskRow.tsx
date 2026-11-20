// Ported from QCE qce-v4-tool/app/page.tsx TASKS and OVERVIEW rows (GPL-3.0).
import {useEffect,useState} from 'react'
import {ChevronDown,FolderOpen,Download,Trash2,Square,FileSearch} from 'lucide-react'
import {formatBytes} from './formatBytes'
import {Button} from '@/components/ui/button'
import {Badge} from '@/components/ui/badge'
import {Progress} from '@/components/ui/progress'
import {call} from './native'

const active=(s:string)=>s==='running'||s==='queued'
const names:Record<string,string>={queued:'等待中',running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断',discarded:'未保存'}
const date=(s:any)=>s?new Date(typeof s==='number'?s*1000:s.replace(' ','T')).toLocaleDateString('zh-CN'):''

type SessionProgress = {name:string; status:string; scanned:number; count:number; total:number|null; detail?:string}
type ProgressPage = {rows:SessionProgress[]; total:number; limit:number}

function BatchSessionProgress({id,status,panelId}:{id:string;status:string;panelId:string}){
 const [offset,setOffset]=useState(0)
 const [page,setPage]=useState<{offset:number; data:ProgressPage}|null>(null)
 const [error,setError]=useState("")
 useEffect(()=>{
  let stopped=false, pending=false
  async function refresh(){
   if(pending)return
   pending=true
   try{
    const data:ProgressPage=await call('task_session_progress',{id,offset})
    if(!stopped){setPage({offset,data});setError("")}
   }catch{
    if(!stopped)setError("会话进度读取失败")
   }finally{pending=false}
  }
  void refresh()
  const timer=active(status)?window.setInterval(refresh,1200):undefined
  return()=>{stopped=true;window.clearInterval(timer)}
 },[id,offset,status])
 const data=page?.offset===offset?page.data:null
 return <div id={panelId} className="mx-4 mb-3 rounded-lg border border-border/70 bg-muted/20 px-3 py-2 text-xs">
  {error&&<p className="py-1.5 text-destructive">{error}</p>}
  {!data&&!error&&<p className="py-1.5 text-muted-foreground">正在读取会话进度…</p>}
  {data&&<>
   <ul aria-label="各会话导出进度" className="max-h-64 divide-y divide-border/60 overflow-y-auto pr-1">
    {data.rows.map((row,index)=>{
     const percent=row.status==='completed'||row.status==='discarded'?100:Math.min(99,Math.floor(row.scanned*100/Math.max(row.total??0,1)))
     const tone=row.status==='failed'?'text-destructive':row.status==='running'?'text-primary':'text-muted-foreground'
     return <li key={offset+index} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-1 py-2.5">
      <span className="truncate font-medium text-foreground" title={row.name}>{row.name}</span>
      <span className={tone}>{names[row.status]}</span>
      <div className="flex items-center gap-2">
       <Progress value={percent} aria-label={row.name+"的导出进度"} className="h-1.5 flex-1 [&_[data-slot=progress-indicator]]:bg-primary"/>
       <span className="w-8 text-right tabular-nums text-muted-foreground">{percent}%</span>
      </div>
      <span className="tabular-nums text-muted-foreground" title={'已导出 '+row.count.toLocaleString()+' 条'}>
       {row.total===null?'待统计':(row.scanned.toLocaleString()+' / '+row.total.toLocaleString()+' 条')}
      </span>
      {row.status==='running'&&row.detail&&<span className="col-span-2 text-muted-foreground">{row.detail}</span>}
     </li>
    })}
   </ul>
   {data.total>data.limit&&<div className="mt-2 flex items-center justify-end gap-2 text-muted-foreground">
    <span>第 {offset/data.limit+1} / {Math.ceil(data.total/data.limit)} 页</span>
    <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" disabled={offset===0} onClick={()=>setOffset(offset-data.limit)}>上一页</Button>
    <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" disabled={offset+data.limit>=data.total} onClick={()=>setOffset(offset+data.limit)}>下一页</Button>
   </div>}
  </>}
 </div>
}

export function TaskRow({task:t,compact=false,onAction,onDelete}:{task:any,compact?:boolean,onAction:(action:string,id:string)=>void,onDelete:(task:any)=>void}){
 const [expanded,setExpanded]=useState(false)
 const panelId='batch-progress-'+t.id
 const unit=t.category==='stickers'?'张表情':t.category==='moments'?'条朋友圈':t.category==='favorites'?'条收藏':compact?'条':'条消息'
 const summary=t.resource_summary,unavailable=(summary?.missing||0)+(summary?.encrypted||0)+(summary?.unsupported||0)+(summary?.corrupt||0)
 const badge=<Badge className={`text-[11px] px-1.5 py-0 rounded-full border-0 ${t.status==='completed'?'text-green-700 bg-green-50 dark:text-green-300 dark:bg-green-950/40':active(t.status)?'text-blue-700 bg-blue-50 dark:text-blue-300 dark:bg-blue-950/40':t.status==='failed'?'text-red-700 bg-red-50 dark:text-red-300 dark:bg-red-950/40':'text-zinc-700 bg-zinc-100 dark:text-zinc-300 dark:bg-zinc-800'}`}>{names[t.status]}</Badge>
 const metadata=<><span>{t.format==='stickers'?'表情包':t.format==='moments'?'朋友圈':t.format==='favorites'?'收藏':t.format?.toUpperCase()}</span><span>{date(t.created_at)}</span>{t.kind==='export'&&<span>{(t.count||0).toLocaleString()} {unit}</span>}{!compact&&t.start_time&&<span className="font-medium">{date(t.start_time)} ~ {date(t.end_time)}</span>}{t.size_info&&<details className="relative"><summary className="cursor-pointer text-primary">{formatBytes(t.size_info.archive_bytes??t.size_info.bytes)}{t.encrypted?' · 加密 ZIP':t.compressed?' · ZIP':''}</summary><div className="absolute z-20 mt-2 min-w-64 rounded-xl border border-border bg-card p-3 text-xs shadow-lg space-y-1"><p>导出文件 {formatBytes(t.size_info.bytes)}</p>{t.size_info.archive_bytes!==undefined&&<p>压缩包 {formatBytes(t.size_info.archive_bytes)}</p>}{Object.entries(t.size_info.resources||{}).map(([kind,bytes])=><p key={kind}>{({image:'图片 / 表情',video:'视频',audio:'语音',file:'文件',original:'原始资源'} as any)[kind]||kind} {formatBytes(Number(bytes))}</p>)}<p className="pt-1 text-muted-foreground">当前账号源数据库总量 {formatBytes(t.size_info.source_db_bytes)}</p><p className="text-muted-foreground">源数据库包含其他会话，导出体积仅对应本次选定内容。</p></div></details>}{summary?.skipped>0&&<span title="按导出设置主动跳过，未计为资源错误">已跳过 {summary.skipped} 项资源</span>}{unavailable>0&&<span className="text-amber-700 dark:text-amber-400" title={`已导出 ${summary.available} 个资源，缺失 ${summary.missing} 个，加密 ${summary.encrypted} 个，格式不支持 ${summary.unsupported||0} 个，校验失败 ${summary.corrupt||0} 个`}>{unavailable} 个资源需处理</span>}</>
 return <div>
 <div className={compact?'flex items-center justify-between px-4 py-3 hover:bg-black/[0.02] dark:hover:bg-white/[0.02] transition-colors':'flex items-center justify-between px-4 py-3.5 text-sm rounded-lg hover:bg-black/[0.03] dark:hover:bg-white/[0.03] transition-colors group'}>
  <div className={'min-w-0 flex-1'+(compact?' flex items-center gap-3':'')}>
   <div className="flex items-center gap-2 min-w-0">
    {t.has_session_progress?<button type="button" aria-expanded={expanded} aria-controls={panelId}
      title={expanded?'收起会话进度':'展开会话进度'} onClick={()=>setExpanded(!expanded)}
      className="flex min-w-0 items-center gap-1.5 rounded text-left text-sm font-medium text-foreground hover:text-primary focus-visible:outline-2 focus-visible:outline-primary">
      <ChevronDown aria-hidden="true" className={'size-3.5 shrink-0 text-muted-foreground transition-transform '+(expanded?'':'-rotate-90')}/>
      <span className="truncate">{t.title}</span>
    </button>:<span className="text-sm font-medium text-foreground truncate">{t.title}</span>}{badge}{t.demo&&<Badge variant="secondary">演示</Badge>}
   </div>
   <div className={compact?'flex items-center gap-3 text-xs text-muted-foreground':'mt-0.5 flex flex-wrap items-center gap-3 text-xs text-muted-foreground/50'}>{metadata}</div>
   {active(t.status)&&<div className="mt-1.5 flex items-center gap-2"><Progress value={t.progress} className="h-1.5 rounded-full flex-1 max-w-[240px]"/><span className="text-xs font-medium text-muted-foreground/50 tabular-nums">{t.progress}%</span></div>}
   {(!compact&&(t.kind!=='export'||t.status==='failed'||active(t.status)||t.session_total>1))&&<div className={'mt-1.5 text-xs '+(t.status==='failed'?'text-red-600':t.session_failed?'text-amber-700 dark:text-amber-400':'text-muted-foreground/50')}>{t.detail}</div>}
  </div>
  <div className="ml-3 flex items-center gap-1.5 flex-shrink-0">
   {t.status==='failed'&&<Button size="sm" variant="ghost" className="h-8 w-8 rounded-full p-0" title="导出此任务的诊断信息" onClick={()=>onAction('choose_diagnostics',t.id)}><FileSearch className="w-4 h-4"/></Button>}
   {active(t.status)&&<Button size="sm" variant="ghost" className="h-8 w-8 rounded-full p-0" title="停止" onClick={()=>onAction('cancel',t.id)}><Square className="w-4 h-4"/></Button>}
   {t.status==='completed'&&t.kind==='export'&&<><Button size="sm" variant="ghost" className="h-8 w-8 rounded-full p-0" title="打开文件位置" onClick={()=>onAction('open_task_folder',t.id)}><FolderOpen className="w-4 h-4"/></Button><Button size="sm" variant="ghost" className="h-8 w-8 rounded-full p-0" title="打开导出文件" onClick={()=>onAction('open_export',t.id)}><Download className="w-4 h-4"/></Button></>}
   {!compact&&!active(t.status)&&<Button size="sm" variant="ghost" className="h-8 w-8 rounded-full p-0 text-muted-foreground/50 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30" title="删除记录" onClick={()=>onDelete(t)}><Trash2 className="w-4 h-4"/></Button>}
  </div>
 </div>
 {expanded&&t.has_session_progress&&<BatchSessionProgress id={t.id} status={t.status} panelId={panelId}/>}
 </div>
}
