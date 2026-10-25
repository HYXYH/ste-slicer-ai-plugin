import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

Column {
    id:view
    objectName:"calibrationExplanation"
    property var backend:null
    property var viewState:({})
    property var entries:viewState.calibrationInfo || []
    property var pricing:(viewState.intelligentCalibration || {}).pricing || ({})
    property var process:entries.length>5 ? entries[5] : ({})
    property bool settingsExpanded:false
    property string scopeKey:(pricing.printer_id || "")+":"+(pricing.materials || []).map(function(m){return m.id;}).join(",")
    property var priceDraft:({})
    property string hourDraft:""
    property bool hourEdited:false
    property string error:""
    signal openKnowledge()
    spacing:14
    onScopeKeyChanged:{priceDraft={};hourDraft="";hourEdited=false;error="";}
    function known(value){return value!==undefined && value!==null;}
    function money(value){return known(value)?Number(value).toFixed(2)+" ₽":"—";}
    function caption(value){
        if(!value || !value.available)return "Появится после нарезки";
        var data=[];
        if(known(value.grams))data.push(Number(value.grams).toFixed(1)+" г");
        if(known(value.hours))data.push(Number(value.hours).toFixed(2)+" ч");
        return data.join(" · ")+(data.length?"\n":"")+(value.reason || "");
    }
    function parse(text){if(!text.trim())return null;var n=Number(text.replace(",","."));if(!isFinite(n)||n<0)throw "Укажите неотрицательные тарифы. Неизвестные значения оставьте пустыми.";return n;}
    function save(){
        error="";
        try {
            var materials={};
            (pricing.materials || []).forEach(function(m){materials[m.id]=parse(priceDraft[m.id]!==undefined?priceDraft[m.id]:(known(m.price)?String(m.price):""));});
            var hour=parse(hourEdited?hourDraft:(known(pricing.printer_hour)?String(pricing.printer_hour):""));
            if(backend)backend.calibrationCommand("tariffs",{tariffs:{printer_id:pricing.printer_id,printer_hour:hour,material_prices:materials}});
        }catch(e){error=String(e);}
    }
    Text { width:parent.width;text:"Небольшая проверочная печать помогает уточнить настройки под ваш принтер и материал. Вы всегда можете продолжить без неё.";color:"#6485a7";font.pixelSize:14;wrapMode:Text.Wrap }
    AssistantCalibrationInfoCard { width:parent.width;entry:view.entries.length?view.entries[0]:({}) }
    Rectangle {
        width:parent.width;implicitHeight:flowBody.height+34;radius:12;color:"#f5fbff";border.color:"#d5e9fb"
        Column {
            id:flowBody;x:18;y:17;width:parent.width-36;spacing:18
            Text { width:parent.width;text:"Как проходит проверка";font.pixelSize:17;font.bold:true;color:"#123d6b";wrapMode:Text.Wrap }
            Flow {
                id:processFlow;width:parent.width;spacing:12
                Repeater {
                    model:view.process.steps || []
                    Item {
                        width:(processFlow.width-((processFlow.width<620)?12:36))/((processFlow.width<620)?2:4);height:154
                        Column {
                            width:parent.width-12;anchors.horizontalCenter:parent.horizontalCenter;spacing:9
                            Rectangle { anchors.horizontalCenter:parent.horizontalCenter;width:58;height:58;radius:29;color:"#e1f2ff";Image { anchors.centerIn:parent;width:30;height:30;source:"icons/"+modelData.icon+".svg" } }
                            Text { width:parent.width;text:modelData.title;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.Wrap;font.bold:true;font.pixelSize:14;color:"#123e6b" }
                            Text { width:parent.width;text:modelData.text;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.Wrap;font.pixelSize:12;color:"#6c8aac" }
                        }
                        Text { visible:index<(view.process.steps || []).length-1 && (processFlow.width>=620 || index%2===0);anchors.right:parent.right;y:14;text:"→";font.pixelSize:28;color:"#8cb7de" }
                    }
                }
            }
            AssistantButton { text:"Подробнее о процессе в базе знаний";compact:true;onClicked:view.openKnowledge() }
        }
    }
    Repeater {
        model:[2,1,4,3,6]
        AssistantCalibrationInfoCard { width:view.width;entry:view.entries[modelData] || ({}) }
    }
    Rectangle {
        width:parent.width;implicitHeight:settingsBody.height+32;radius:12;color:"#edf7ff";border.color:"#c8e3fb"
        Column {
            id:settingsBody;x:18;y:16;width:parent.width-36;spacing:14
            RowLayout {
                width:parent.width
                Image { source:"icons/Settings.svg";Layout.preferredWidth:25;Layout.preferredHeight:25 }
                Text { Layout.fillWidth:true;text:"Настройки";font.bold:true;font.pixelSize:17;color:"#16416d" }
                AssistantButton { text:view.settingsExpanded?"Свернуть":"Раскрыть";compact:true;onClicked:view.settingsExpanded=!view.settingsExpanded }
            }
            Text { width:parent.width;text:"Тарифы материала и принтера для расчёта стоимости печати";font.pixelSize:12;color:"#6b8bab";wrapMode:Text.Wrap }
            Column {
                width:parent.width;spacing:12;visible:view.settingsExpanded
                Text { width:parent.width;text:"Стоимость = граммы пластика × цена за грамм + часы печати × цена за час. Расход и время берём из нарезки. Тарифы сохраняются для материала и принтера и доступны в следующих задачах.";font.pixelSize:13;wrapMode:Text.Wrap;color:"#5f82a4" }
                Repeater {
                    model:view.pricing.materials || []
                    Column {
                        width:settingsBody.width;spacing:6
                        Text { width:parent.width;text:"Цена пластика · "+modelData.title+", ₽/г";font.bold:true;font.pixelSize:13;color:"#214b74";wrapMode:Text.Wrap }
                        TextField { width:parent.width;placeholderText:"Например, 2,5";text:view.priceDraft[modelData.id]!==undefined?view.priceDraft[modelData.id]:(view.known(modelData.price)?String(modelData.price):"");onTextEdited:{var values={};Object.keys(view.priceDraft).forEach(function(k){values[k]=view.priceDraft[k];});values[modelData.id]=text;view.priceDraft=values;} }
                    }
                }
                Text { width:parent.width;text:"Работа принтера · "+(view.pricing.printer_title || "выберите принтер")+", ₽/час";font.bold:true;font.pixelSize:13;color:"#214b74";wrapMode:Text.Wrap }
                TextField { width:parent.width;placeholderText:"Например, 50";text:view.hourEdited?view.hourDraft:(view.known(view.pricing.printer_hour)?String(view.pricing.printer_hour):"");onTextEdited:{view.hourDraft=text;view.hourEdited=true;} }
                Text { width:parent.width;text:"Можно учесть электричество, износ и обслуживание принтера. Если затраты неизвестны, оставьте поле пустым. Ноль означает, что эта часть затрат не учитывается.";font.pixelSize:11;wrapMode:Text.Wrap;color:"#7895af" }
                AssistantButton { text:"Сохранить тарифы";primary:true;enabled:!!view.pricing.printer_id;onClicked:view.save() }
                Text { width:parent.width;visible:view.error!=="" || !!view.viewState.notice;text:view.error || view.viewState.notice || "";textFormat:Text.PlainText;font.pixelSize:12;wrapMode:Text.Wrap;color:view.error?"#b66b3b":"#4f8aa3" }
                Flow {
                    width:parent.width;spacing:12
                    Repeater {
                        model:[{title:"Основная печать",value:view.pricing.main || ({})},{title:"Проверочный тест",value:view.pricing.test || ({})}]
                        Rectangle {
                            width:settingsBody.width<550?settingsBody.width:(settingsBody.width-12)/2;height:138;radius:9;color:"white";border.color:"#d7e9f6"
                            Column { x:14;y:14;width:parent.width-28;spacing:8
                                Text { width:parent.width;text:modelData.title;font.bold:true;font.pixelSize:13;color:"#234b73" }
                                Text { text:view.money(modelData.value.cost);font.bold:true;font.pixelSize:21;color:"#1270c6" }
                                Text { width:parent.width;text:view.caption(modelData.value);font.pixelSize:11;color:"#7894ad";wrapMode:Text.Wrap }
                            }
                        }
                    }
                }
                Text { width:parent.width;text:"Это расчёт по прогнозу слайсера. Фактические время и расход могут отличаться. Стоимость теста появится после его нарезки; она не оценивается по размерам STL.";font.pixelSize:11;wrapMode:Text.Wrap;color:"#7895af" }
            }
        }
    }
}
