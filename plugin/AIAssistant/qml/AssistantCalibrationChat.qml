import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

Rectangle {
    id:card
    property var backend:null
    property var viewState:({})
    property var value:viewState.intelligentCalibration || ({})
    property var decision:value.decision || ({})
    property var run:value.run || ({})
    property bool active:["preparing","test_ready","awaiting_print","review"].indexOf(run.state) >= 0
    property var entries:run.prints || []
    property var entry:entries.length ? entries[Math.min(Math.max(0,printSelect.currentIndex),entries.length-1)] : ({})
    property var observation:(entry.observations || []).length ? entry.observations[entry.observations.length-1].data : ({})
    property var trials:[].concat.apply([],entries.map(function(p){return p.adjustments || [];}))
    property var trial:trials.length ? trials[Math.min(Math.max(0,trialSelect.currentIndex),trials.length-1)] : ({})
    property var trialData:trial.data || ({})
    property var selected:[]
    property var checks:({})
    property var photoIds:[]
    property bool expanded:false
    property string error:""
    signal explain()
    implicitHeight:content.height+28
    color:"#f3faff"; border.color:"#b7d9ee"; radius:10
    function command(name,body){if(backend)backend.calibrationCommand(name,body);}
    function feedback(id,name,body){if(backend)backend.calibrationFeedback(id,name,body);}
    function toggle(list,id,on){var next=list.slice(0),at=next.indexOf(id);if(on&&at<0)next.push(id);else if(!on&&at>=0)next.splice(at,1);return next;}
    function title(id){return ({adhesion:"Прилипание и первый слой",extrusion:"Пропуски / избыток пластика",dimensions:"Размеры и посадки",stringing:"Нити при переходах",bridges:"Провисание мостов",overhangs:"Нависания",small_features:"Мелкие элементы",bonding:"Межслойное сцепление"})[id] || id;}
    function sizes(text){return text.split(/[;\s]+/).filter(function(v){return v!=="";}).map(function(v){var n=Number(v.replace(",","."));if(!isFinite(n)||n<=0)throw "Укажите три положительных размера в мм";return n;});}
    function save(){
        error="";
        try {
            if(!printed.checked)throw "Подтвердите физическую печать выбранного расчёта.";
            var operations=run.operations || [];
            if(!operations.length)throw "Сначала завершите расчёт теста.";
            var measurements={};
            if(actual.text.trim()) {measurements.actual_mm=sizes(actual.text);if(measurements.actual_mm.length!==3)throw "Нужно три измеренных размера";}
            if(expected.text.trim()) {measurements.expected_mm=sizes(expected.text);if(measurements.expected_mm.length!==3)throw "Нужно три номинальных размера";}
            var statuses=Object.keys(checks).map(function(k){return checks[k];});
            var result=statuses.indexOf("fail")>=0 || selected.length ? "defects" : statuses.indexOf("pass")>=0 ? "good" : statuses.indexOf("unknown")>=0 ? "unknown" : "not_checked";
            feedback("","report",{operation_id:operations[Math.max(0,operationSelect.currentIndex)].id,printed:true,result:result,checks:checks,defects:selected,text:note.text,
                answers:{external:external.checked?"Проверены":"Не проверены"},measurements:measurements,photo_ids:photoIds,
                llm_photo_consent:photoConsent.checked,llm_recipient:viewState.llmRecipient || ""});
            printed.checked=false;
        }catch(e){error=String(e);}
    }
    Column {
        id:content;x:14;y:14;width:parent.width-28;spacing:10
        RowLayout {
            width:parent.width
            Text { Layout.fillWidth:true;text:card.active ? "Калибровка · " + ((card.run.test || {}).title || "") : "Необязательная калибровка";color:"#225779";font.bold:true;font.pixelSize:15;wrapMode:Text.Wrap }
            AssistantButton { text:"i";compact:true;onClicked:card.explain() }
        }
        Text { width:parent.width;text:card.decision.reason || "";textFormat:Text.PlainText;wrapMode:Text.Wrap;color:"#607e96";font.pixelSize:12 }
        Text { width:parent.width;visible:!card.active;text:"Недостаточно проверено: " + (card.decision.unchecked_labels || []).join("; ");color:"#8d754f";font.pixelSize:11;wrapMode:Text.Wrap }
        Text { width:parent.width;visible:!card.active && !!card.decision.test;text:"Выбран тест: " + ((card.decision.test || {}).title || "") + ". Проверки: " + ((card.decision.test || {}).checks || []).map(card.title).join("; ");color:"#5e8aa5";font.pixelSize:11;wrapMode:Text.Wrap }
        Flow {
            width:parent.width;spacing:8;visible:!card.active
            AssistantButton { text:"Провести калибровку";primary:true;onClicked:card.command("start",{decision_id:card.decision.id}) }
            AssistantButton { text:"Продолжить без калибровки";onClicked:card.command("skip",{decision_id:card.decision.id}) }
        }
        Column {
            width:parent.width;spacing:10;visible:card.active
            Text { width:parent.width;text:"1. Сохраните основной проект. 2. Подготовьте план теста. 3. Подтвердите его справа, выполните расчёт и экспорт. Физическую печать запускаете вы.";color:"#7c7392";wrapMode:Text.Wrap;font.pixelSize:12 }
            Text { width:parent.width;text:(card.run.test || {}).limitations || "";color:"#8e815f";font.pixelSize:11;wrapMode:Text.Wrap }
            AssistantButton { text:"Подготовить / обновить план теста";visible:card.run.state!=="review";onClicked:card.command("prepare",{}) }
            AssistantButton { visible:!!card.run.file_id;text:"Повторно загрузить тест из вложения";compact:true;onClicked:if(card.backend)card.backend.importFile(card.run.file_id) }
            Text { width:parent.width;text:"После физической печати";font.bold:true;font.pixelSize:13;color:"#2c6687" }
            ComboBox { id:operationSelect;width:parent.width;model:(card.run.operations || []).map(function(o){return o.id + " · " + o.created_at.substring(0,16);});onActivated:printed.checked=false }
            CheckBox { id:printed;text:"Я напечатал выбранный расчёт и осматриваю деталь до зачистки";width:parent.width;font.pixelSize:11 }
            Repeater {
                model:(card.run.test || {}).checks || []
                Column {
                    width:content.width;spacing:3
                    Text { width:parent.width;text:card.title(modelData);wrapMode:Text.Wrap;color:"#517c98";font.pixelSize:12 }
                    ComboBox { width:parent.width;model:["Не проверялось","Дефектов нет","Есть дефект","Не могу определить"];onActivated:{var next={};Object.keys(card.checks).forEach(function(k){next[k]=card.checks[k];});next[modelData]=["not_checked","pass","fail","unknown"][currentIndex];card.checks=next;} }
                }
            }
            AssistantButton { text:card.expanded?"Скрыть признаки дефектов":"Уточнить видимые дефекты";compact:true;onClicked:card.expanded=!card.expanded }
            Column {
                width:parent.width;visible:card.expanded
                Repeater { model:card.run.catalog || [];CheckBox { width:content.width;text:modelData.label;font.pixelSize:11;checked:card.selected.indexOf(modelData.id)>=0;onClicked:card.selected=card.toggle(card.selected,modelData.id,checked) } }
            }
            TextArea { id:note;width:parent.width;height:85;placeholderText:"Опишите, где и как проявляется проблема. Можно написать «не могу определить».";wrapMode:TextEdit.Wrap }
            Column {
                width:parent.width;visible:((card.run.test || {}).checks || []).indexOf("dimensions")>=0;spacing:5
                Text { width:parent.width;text:"Размеры X Y Z, мм. Номинал берите из проверяемого элемента модели; пустые поля — не измерялось. Для куба номинал 20 20 20.";font.pixelSize:11;wrapMode:Text.Wrap;color:"#6e8ca3" }
                TextField { id:expected;width:parent.width;placeholderText:"Номинальные X Y Z" }
                TextField { id:actual;width:parent.width;placeholderText:"Измеренные X Y Z" }
            }
            Text { width:parent.width;text:"Проверьте стол и зазор первого слоя, сухость пластика, подачу, засор и механику. Эти причины могут требовать обслуживания, а не изменения настроек.";color:"#a38150";font.pixelSize:11;wrapMode:Text.Wrap }
            CheckBox { id:external;width:parent.width;text:"Внешние причины проверены";font.pixelSize:11 }
            AssistantButton { text:"Прикрепить фотографии";compact:true;onClicked:if(card.backend)card.backend.attachFiles() }
            Repeater { model:card.viewState.photoFiles || [];CheckBox { width:content.width;text:modelData.name;font.pixelSize:11;checked:card.photoIds.indexOf(modelData.id)>=0;onClicked:card.photoIds=card.toggle(card.photoIds,modelData.id,checked) } }
            CheckBox {
                id:photoConsent;width:parent.width;text:"Разрешаю отправить выбранные фото: " + (card.viewState.llmRecipient || "LLM не настроена");font.pixelSize:10
                contentItem:Text { text:photoConsent.text;wrapMode:Text.Wrap;font:photoConsent.font;color:"#64879d";leftPadding:photoConsent.indicator.width+photoConsent.spacing }
                onCheckedChanged:if(card.photoIds.length===0)checked=false
            }
            AssistantButton { text:"Сохранить результат физической печати";primary:true;onClicked:card.save() }
            Text { width:parent.width;visible:card.error!=="";text:card.error;wrapMode:Text.Wrap;color:"#b36b42";font.pixelSize:12 }
            Text { width:parent.width;visible:!!card.run.visual;text:(card.run.visual || {}).text || "";textFormat:Text.PlainText;wrapMode:Text.Wrap;color:"#5c849f";font.pixelSize:12 }
            ComboBox { id:printSelect;width:parent.width;model:card.entries.map(function(p){return "Печать " + p.id.substring(0,8) + " · " + p.created_at.substring(0,16);}) }
            Text { width:parent.width;text:card.observation.text || "";textFormat:Text.PlainText;wrapMode:Text.Wrap;color:"#7394ab";font.pixelSize:11 }
            Column {
                width:parent.width;spacing:6;visible:(card.observation.defects || []).length > 0
                Text { width:parent.width;text:"Уточните признак; варианты — альтернативные гипотезы. Проверяйте один параметр за раз.";color:"#7a8c9e";font.pixelSize:11;wrapMode:Text.Wrap }
                Repeater {
                    model:(card.run.catalog || []).filter(function(d){return (card.observation.defects || []).indexOf(d.id)>=0;})
                    Column {
                        property var defect:modelData
                        width:content.width;spacing:5
                        Text { width:parent.width;text:modelData.label;color:"#3d7696";font.pixelSize:12;wrapMode:Text.Wrap }
                        ComboBox { id:symptom;width:parent.width;model:(defect.choices || []).map(function(c){return c.symptom;}) }
                        Text { width:parent.width;text:((defect.choices || [])[symptom.currentIndex] || {}).reason || "";wrapMode:Text.Wrap;color:"#8d825e";font.pixelSize:11 }
                        AssistantButton { text:"Предложить изменение одного параметра";compact:true;onClicked:card.feedback(card.entry.id,"diagnose",{defect:defect.id,choice:String(symptom.currentIndex),external_checked:external.checked}) }
                    }
                }
            }
            Column {
                width:parent.width;spacing:8;visible:card.trials.length > 0
                ComboBox { id:trialSelect;width:parent.width;model:card.trials.map(function(a){return a.data.key + ": " + a.data.before + " → " + a.data.after;}) }
                Text { width:parent.width;text:card.trialData.reason || "";wrapMode:Text.Wrap;color:"#4c7a96";font.pixelSize:12 }
                CheckBox { id:consent;width:parent.width;text:"Согласен на указанное изменение / откат одного параметра";font.pixelSize:11 }
                AssistantButton { text:"Применить изменение";visible:card.trialData.state==="proposed";enabled:consent.checked;onClicked:{card.feedback(card.trial.id,"apply",{consent:true});consent.checked=false;} }
                AssistantButton { text:"Подготовить повторную печать";visible:card.trialData.state==="applied";onClicked:card.feedback(card.trial.id,"repeat-plan",{}) }
                Text { width:parent.width;text:"Сохраните результат новой физической печати, выберите её выше и укажите эффект исправления.";wrapMode:Text.Wrap;color:"#708fa5";font.pixelSize:11 }
                ComboBox { id:outcome;width:parent.width;model:["Дефект исчез","Стало лучше","Стало хуже","Без изменений","Не проверялось","Не могу определить"] }
                AssistantButton { text:"Сохранить результат повторной проверки";visible:card.trialData.state==="applied";onClicked:card.feedback(card.trial.id,"evaluate",{repeat_print_id:card.entry.id,outcome:["resolved","better","worse","unchanged","not_checked","unknown"][outcome.currentIndex]}) }
                Text { width:parent.width;visible:["worse","unchanged"].indexOf(card.trialData.outcome)>=0;text:"Изменение не помогло. Предлагаю откатить его и проверить другой признак из дерева причин.";color:"#b1834d";font.pixelSize:11;wrapMode:Text.Wrap }
                AssistantButton { text:"Вернуть исходное значение";visible:["applied","evaluated"].indexOf(card.trialData.state)>=0;enabled:consent.checked;onClicked:{card.feedback(card.trial.id,"rollback",{consent:true});consent.checked=false;} }
            }
            Flow {
                width:parent.width;spacing:8
                AssistantButton { text:"Завершить и вернуться к детали";onClicked:card.command("finish",{}) }
                AssistantButton { text:"Отменить калибровку";onClicked:card.command("cancel",{}) }
            }
        }
    }
}
