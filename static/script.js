const API = "http://127.0.0.1:5000";

const categorySelect = document.getElementById("category");
const testSelect = document.getElementById("test");
const subjectSelect = document.getElementById("subject");


// LOAD CATEGORIES
async function loadCategories(){

let res = await fetch(API + "/categories");
let data = await res.json();

data.forEach(cat => {

let option = document.createElement("option");
option.value = cat.uuid;
option.text = cat.title;

categorySelect.appendChild(option);

});

}

loadCategories();


// LOAD TESTS
categorySelect.addEventListener("change", async function(){

let categoryUUID = this.value;

testSelect.innerHTML = "<option>Select Test</option>";
subjectSelect.innerHTML = "<option>Select Subject</option>";

let res = await fetch(API + "/tests/" + categoryUUID);
let tests = await res.json();

tests.forEach(test => {

let option = document.createElement("option");
option.value = test.uuid;
option.text = test.title;

testSelect.appendChild(option);

});

});


// LOAD SUBJECTS
testSelect.addEventListener("change", async function(){

let testUUID = this.value;

subjectSelect.innerHTML = "<option>Select Subject</option>";

let res = await fetch(API + "/subjects/" + testUUID);
let subjects = await res.json();

subjects.forEach(sub => {

let option = document.createElement("option");
option.value = sub.id;
option.text = sub.uuid;

subjectSelect.appendChild(option);

});

});


// UPLOAD DOC
async function uploadDoc(){

let file = document.getElementById("docfile").files[0];

if(!file){
alert("Please select DOC file");
return;
}

let formData = new FormData();
formData.append("file", file);

let res = await fetch(API + "/upload", {
method: "POST",
body: formData
});

let data = await res.json();

alert("Uploaded " + data.count + " questions");

}