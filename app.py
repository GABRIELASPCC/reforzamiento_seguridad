import base64
import io
from datetime import datetime

import bcrypt
import pandas as pd
import psycopg2
import streamlit as st
from PIL import Image
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:
    CANVAS_AVAILABLE = False

st.set_page_config(
    page_title="Sistema de Mini Lecciones",
    page_icon="🎓",
    layout="wide"
)


def get_connection():
    return psycopg2.connect(
        st.secrets["DATABASE_URL"],
        sslmode="require"
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8")
    )


def get_lessons() -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT
                l.id,
                l.title AS lesson_title,
                l.content,
                l.video_url,
                l.duration_minutes,
                l.order_index,
                m.title AS module_title
            FROM lessons l
            INNER JOIN modules m ON m.id = l.module_id
            ORDER BY m.order_index, l.order_index, l.id;
        """
        return pd.read_sql_query(query, conn)
    finally:
        conn.close()


def get_questions(lesson_id: int) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT *
            FROM questions
            WHERE lesson_id = %s
            ORDER BY order_index, id;
        """
        return pd.read_sql_query(query, conn, params=(lesson_id,))
    finally:
        conn.close()


def get_user_progress(user_id: int) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = """
            SELECT
                l.title AS leccion,
                m.title AS modulo,
                up.best_score AS mejor_puntaje,
                up.is_completed AS completado,
                up.completed_at AS fecha_finalizacion
            FROM user_progress up
            INNER JOIN lessons l ON l.id = up.lesson_id
            INNER JOIN modules m ON m.id = l.module_id
            WHERE up.user_id = %s
            ORDER BY m.order_index, l.order_index;
        """
        return pd.read_sql_query(query, conn, params=(user_id,))
    finally:
        conn.close()


def register_student(registration_code: str, first_name: str, last_name: str):
    registration_code = registration_code.strip()
    first_name = first_name.strip()
    last_name = last_name.strip()

    if not registration_code or not first_name or not last_name:
        return False, "Completa el número de registro, nombre y apellido."

    if len(registration_code) < 4:
        return False, "El número de registro debe tener al menos 4 caracteres."

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT id FROM users WHERE registration_code = %s;",
            (registration_code,)
        )

        if cur.fetchone():
            return False, "Este número de registro ya existe. Inicia sesión."

        cur.execute(
            """
            INSERT INTO users (
                registration_code,
                first_name,
                last_name,
                password_hash,
                role,
                must_change_password
            )
            VALUES (%s, %s, %s, %s, 'student', FALSE);
            """,
            (
                registration_code,
                first_name,
                last_name,
                hash_password(registration_code)
            )
        )

        conn.commit()
        return True, (
            "Cuenta creada correctamente. Tu usuario y contraseña "
            "son tu número de registro."
        )
    except Exception as error:
        if conn:
            conn.rollback()
        return False, f"Error al registrar: {error}"
    finally:
        if conn:
            conn.close()


def login_student(registration_code: str, password: str):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                registration_code,
                first_name,
                last_name,
                password_hash,
                role
            FROM users
            WHERE registration_code = %s;
            """,
            (registration_code.strip(),)
        )

        user = cur.fetchone()

        if not user:
            return None, "No existe un usuario con ese número de registro."

        if not verify_password(password, user[4]):
            return None, "Número de registro o contraseña incorrectos."

        return {
            "id": user[0],
            "registration_code": user[1],
            "first_name": user[2],
            "last_name": user[3],
            "role": user[5]
        }, None
    except Exception as error:
        return None, f"Error al iniciar sesión: {error}"
    finally:
        if conn:
            conn.close()


def image_to_base64(image_data) -> str:
    image = Image.fromarray(image_data.astype("uint8"))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def uploaded_image_to_base64(uploaded_file) -> str:
    return base64.b64encode(uploaded_file.getvalue()).decode("utf-8")


def save_quiz_attempt(user_id: int, lesson_id: int, questions: pd.DataFrame, student_answers: dict):
    total_points = 0
    obtained_points = 0
    drawing_was_sent = False

    for _, question in questions.iterrows():
        question_id = int(question["id"])
        question_type = question["question_type"]
        points = int(question["points"])
        answer = student_answers.get(question_id, {})

        if question_type == "multiple_choice":
            total_points += points
            if answer.get("selected_option") == question["correct_option"]:
                obtained_points += points

        elif question_type == "drawing":
            total_points += points
            if answer.get("image_base64"):
                obtained_points += points
                drawing_was_sent = True

    score = round((obtained_points / total_points) * 100, 2) if total_points else 0
    passed = score >= 70

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO quiz_attempts (user_id, lesson_id, score, passed)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
            """,
            (user_id, lesson_id, score, passed)
        )
        attempt_id = cur.fetchone()[0]

        for _, question in questions.iterrows():
            question_id = int(question["id"])
            question_type = question["question_type"]
            points = int(question["points"])
            answer = student_answers.get(question_id, {})

            if question_type == "multiple_choice":
                selected_option = answer.get("selected_option")
                is_correct = selected_option == question["correct_option"]
                points_obtained = points if is_correct else 0

                cur.execute(
                    """
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, %s, NULL, %s, %s);
                    """,
                    (
                        attempt_id,
                        question_id,
                        selected_option,
                        is_correct,
                        points_obtained
                    )
                )

            elif question_type == "open_text":
                cur.execute(
                    """
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, NULL, %s, NULL, 0);
                    """,
                    (
                        attempt_id,
                        question_id,
                        answer.get("text_answer", "")
                    )
                )

            elif question_type == "drawing":
                image_base64 = answer.get("image_base64")

                if image_base64:
                    cur.execute(
                        """
                        INSERT INTO drawing_answers (
                            attempt_id,
                            question_id,
                            image_base64,
                            teacher_feedback,
                            reviewed,
                            points_obtained
                        )
                        VALUES (%s, %s, %s, %s, TRUE, %s);
                        """,
                        (
                            attempt_id,
                            question_id,
                            image_base64,
                            "Dibujo recibido y aceptado automáticamente.",
                            points
                        )
                    )

        cur.execute(
            """
            INSERT INTO user_progress (
                user_id,
                lesson_id,
                best_score,
                is_completed,
                completed_at,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (user_id, lesson_id)
            DO UPDATE SET
                best_score = GREATEST(user_progress.best_score, EXCLUDED.best_score),
                is_completed = user_progress.is_completed OR EXCLUDED.is_completed,
                completed_at = CASE
                    WHEN user_progress.completed_at IS NULL AND EXCLUDED.is_completed
                    THEN CURRENT_TIMESTAMP
                    ELSE user_progress.completed_at
                END,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (user_id, lesson_id, score, passed, passed)
        )

        conn.commit()
        return True, {
            "score": score,
            "passed": passed,
            "drawing_was_sent": drawing_was_sent
        }
    except Exception as error:
        if conn:
            conn.rollback()
        return False, f"Error al guardar la evaluación: {error}"
    finally:
        if conn:
            conn.close()


def prepare_progress_for_excel(progress: pd.DataFrame) -> pd.DataFrame:
    progress_excel = progress.copy()

    for column in progress_excel.columns:
        if pd.api.types.is_datetime64tz_dtype(progress_excel[column]):
            progress_excel[column] = progress_excel[column].dt.tz_localize(None)

        progress_excel[column] = progress_excel[column].apply(
            lambda value: str(value)
            if isinstance(value, (list, dict, tuple, set))
            else value
        )

    return progress_excel


def generate_progress_pdf(full_name: str, registration_code: str, progress_df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=landscape(letter))

    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawCentredString(396, 540, "REPORTE DE PROGRESO")

    pdf.setFont("Helvetica", 12)
    pdf.drawString(70, 495, f"Estudiante: {full_name}")
    pdf.drawString(70, 475, f"Registro: {registration_code}")
    pdf.drawString(
        70,
        455,
        f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    y = 410
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(70, y, "Módulo")
    pdf.drawString(230, y, "Lección")
    pdf.drawString(490, y, "Puntaje")
    pdf.drawString(580, y, "Estado")

    pdf.setFont("Helvetica", 10)
    y -= 25

    for _, row in progress_df.iterrows():
        if y < 70:
            pdf.showPage()
            y = 520
            pdf.setFont("Helvetica", 10)

        module_name = str(row.get("modulo", ""))[:22]
        lesson_name = str(row.get("leccion", ""))[:35]
        score_value = row.get("mejor_puntaje", 0)

        if pd.isna(score_value):
            score_value = 0

        score = f"{float(score_value):.0f}%"
        status = "Completado" if bool(row.get("completado", False)) else "Pendiente"

        pdf.drawString(70, y, module_name)
        pdf.drawString(230, y, lesson_name)
        pdf.drawString(490, y, score)
        pdf.drawString(580, y, status)
        y -= 22

    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.title("🎓 Sistema de Mini Lecciones")
    st.write(
        "Regístrate con tu número de registro, nombre y apellido. "
        "Tu usuario y contraseña inicial son tu número de registro."
    )

    tab_login, tab_register = st.tabs(["🔐 Iniciar sesión", "📝 Registrarse"])

    with tab_login:
        st.subheader("Acceso al sistema")

        login_registration = st.text_input(
            "Número de registro",
            key="login_registration"
        )
        login_password = st.text_input(
            "Contraseña",
            type="password",
            key="login_password"
        )

        if st.button("Ingresar", type="primary", use_container_width=True):
            user, error = login_student(login_registration, login_password)

            if error:
                st.error(error)
            else:
                st.session_state.user = user
                st.rerun()

    with tab_register:
        st.subheader("Registro de estudiante")

        registration_code = st.text_input(
            "Número de registro o código institucional",
            key="register_code"
        )
        first_name = st.text_input("Nombre", key="register_name")
        last_name = st.text_input("Apellido", key="register_last_name")

        st.info(
            "La contraseña para ingresar será el mismo número de registro."
        )

        if st.button("Crear cuenta", use_container_width=True):
            success, message = register_student(
                registration_code,
                first_name,
                last_name
            )

            if success:
                st.success(message)
            else:
                st.error(message)

else:
    user = st.session_state.user
    user_full_name = f"{user['first_name']} {user['last_name']}"

    st.sidebar.title("🎓 Mini Lecciones")
    st.sidebar.write(f"**Estudiante:** {user_full_name}")
    st.sidebar.caption(f"Registro: {user['registration_code']}")

    menu = st.sidebar.radio(
        "Navegación",
        ["📚 Lecciones", "📊 Mi progreso", "🚪 Cerrar sesión"]
    )

    if menu == "🚪 Cerrar sesión":
        st.session_state.user = None
        st.rerun()

    elif menu == "📊 Mi progreso":
        st.header("📊 Mi progreso académico")
        progress = get_user_progress(user["id"])

        if progress.empty:
            st.info(
                "Aún no tienes evaluaciones registradas. "
                "Ingresa a una lección y responde su cuestionario."
            )
        else:
            total_lessons = len(progress)
            completed = int(progress["completado"].sum())
            average = float(progress["mejor_puntaje"].mean())
            completion_percent = (completed / total_lessons) * 100 if total_lessons else 0

            col1, col2, col3 = st.columns(3)
            col1.metric("Lecciones completadas", f"{completed}/{total_lessons}")
            col2.metric("Avance", f"{completion_percent:.1f}%")
            col3.metric("Promedio", f"{average:.1f}%")

            st.dataframe(progress, use_container_width=True)

            progress_excel = prepare_progress_for_excel(progress)
            excel_buffer = io.BytesIO()

            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                progress_excel.to_excel(
                    writer,
                    index=False,
                    sheet_name="Progreso"
                )

            st.download_button(
                label="📥 Descargar mi progreso en Excel",
                data=excel_buffer.getvalue(),
                file_name=f"progreso_{user['registration_code']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                on_click="ignore"
            )

            pdf_data = generate_progress_pdf(
                user_full_name,
                user["registration_code"],
                progress
            )

            st.download_button(
                label="📄 Descargar mi progreso en PDF",
                data=pdf_data,
                file_name=f"progreso_{user['registration_code']}.pdf",
                mime="application/pdf",
                on_click="ignore"
            )

    elif menu == "📚 Lecciones":
        st.header("📚 Módulos y mini lecciones")

        lessons = get_lessons()

        if lessons.empty:
            st.warning(
                "No hay lecciones disponibles. Agrega módulos y lecciones "
                "desde el SQL Editor de Supabase."
            )
        else:
            lessons["label"] = (
                lessons["module_title"].astype(str)
                + " — "
                + lessons["lesson_title"].astype(str)
            )

            selected_label = st.selectbox(
                "Selecciona una lección",
                lessons["label"]
            )

            lesson = lessons[lessons["label"] == selected_label].iloc[0]

            st.subheader(lesson["lesson_title"])
            st.caption(
                f"Módulo: {lesson['module_title']} | "
                f"Duración estimada: {lesson['duration_minutes']} minutos"
            )

            if lesson["video_url"] and pd.notna(lesson["video_url"]):
                st.video(lesson["video_url"])

            if lesson["content"] and pd.notna(lesson["content"]):
                st.markdown(lesson["content"])

            st.divider()
            st.subheader("📝 Evaluación interactiva")

            questions = get_questions(int(lesson["id"]))

            if questions.empty:
                st.info("Esta lección todavía no tiene preguntas.")
            else:
                st.write(
                    "Las alternativas se califican automáticamente. "
                    "Cualquier dibujo enviado se acepta como correcto."
                )

                student_answers = {}

                for index, question in questions.iterrows():
                    question_id = int(question["id"])
                    question_type = question["question_type"]

                    st.markdown(
                        f"### Pregunta {index + 1}: {question['question_text']}"
                    )

                    if question_type == "multiple_choice":
                        options = []

                        if pd.notna(question["option_a"]):
                            options.append(f"A) {question['option_a']}")
                        if pd.notna(question["option_b"]):
                            options.append(f"B) {question['option_b']}")
                        if pd.notna(question["option_c"]):
                            options.append(f"C) {question['option_c']}")
                        if pd.notna(question["option_d"]):
                            options.append(f"D) {question['option_d']}")

                        selected = st.radio(
                            "Selecciona una respuesta:",
                            options,
                            key=f"choice_{question_id}"
                        )

                        student_answers[question_id] = {
                            "selected_option": selected[0]
                        }

                    elif question_type == "open_text":
                        text_response = st.text_area(
                            "Escribe tu respuesta:",
                            key=f"text_{question_id}",
                            height=120
                        )

                        student_answers[question_id] = {
                            "text_answer": text_response
                        }

                    elif question_type == "drawing":
                        st.info(
                            "Dibuja en el recuadro. Cualquier dibujo enviado "
                            "se aceptará como correcto."
                        )

                        image_base64 = None

                        if CANVAS_AVAILABLE:
                            drawing_mode = st.selectbox(
                                "Herramienta de dibujo",
                                ["freedraw", "line", "rect", "circle"],
                                key=f"mode_{question_id}"
                            )

                            stroke_width = st.slider(
                                "Grosor del pincel",
                                min_value=1,
                                max_value=20,
                                value=4,
                                key=f"width_{question_id}"
                            )

                            stroke_color = st.color_picker(
                                "Color del pincel",
                                "#000000",
                                key=f"color_{question_id}"
                            )

                            canvas_result = st_canvas(
                                fill_color="rgba(255, 255, 255, 0.0)",
                                stroke_width=stroke_width,
                                stroke_color=stroke_color,
                                background_color="#FFFFFF",
                                height=350,
                                width=700,
                                drawing_mode=drawing_mode,
                                display_toolbar=True,
                                update_streamlit=True,
                                key=f"canvas_{question_id}"
                            )

                            if canvas_result.image_data is not None:
                                image_base64 = image_to_base64(
                                    canvas_result.image_data
                                )
                        else:
                            st.warning(
                                "No se pudo cargar el lienzo de dibujo. "
                                "Puedes subir una imagen como alternativa."
                            )

                            uploaded = st.file_uploader(
                                "Sube una imagen de tu dibujo",
                                type=["png", "jpg", "jpeg"],
                                key=f"upload_{question_id}"
                            )

                            if uploaded is not None:
                                image_base64 = uploaded_image_to_base64(uploaded)
                                st.image(uploaded)

                        student_answers[question_id] = {
                            "image_base64": image_base64
                        }

                    st.divider()

                if st.button(
                    "✅ Enviar evaluación",
                    type="primary",
                    use_container_width=True
                ):
                    success, result = save_quiz_attempt(
                        user["id"],
                        int(lesson["id"]),
                        questions,
                        student_answers
                    )

                    if success:
                        st.success(
                            f"Evaluación registrada. "
                            f"Puntaje: {result['score']:.0f}%"
                        )

                        if result.get("drawing_was_sent"):
                            st.info(
                                "✅ Tu dibujo fue recibido y aceptado "
                                "como correcto."
                            )

                        if result["passed"]:
                            st.balloons()
                            st.success("🎉 ¡Aprobaste la mini lección!")
                        else:
                            st.warning(
                                "Aún no alcanzas el 70%. "
                                "Puedes volver a intentar el cuestionario."
                            )
                    else:
                        st.error(result)
