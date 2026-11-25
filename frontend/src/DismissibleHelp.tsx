import {useState,type ReactNode} from 'react'
import {X} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {toast} from '@/components/ui/toast'

export function DismissibleHelp({children,dismissLabel,onDismiss}:{
  children:ReactNode
  dismissLabel:string
  onDismiss:()=>Promise<void>
}){
  const [saving,setSaving]=useState(false)
  const dismiss=async()=>{
    setSaving(true)
    try{await onDismiss()}
    catch(error){toast.error('设置保存失败',{description:(error as Error).message})}
    finally{setSaving(false)}
  }

  return <div className="flex items-center gap-2 text-muted-foreground">
    <p className="min-w-0 text-xs leading-relaxed">{children}</p>
    <Button type="button" variant="ghost" size="icon" className="size-6 shrink-0 rounded-md hover:text-primary"
      title={dismissLabel} aria-label={dismissLabel} disabled={saving} onClick={dismiss}>
      <X aria-hidden="true" className="size-3.5"/>
    </Button>
  </div>
}
