import base64
import io
import uuid
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

# ==========================================================
# CONEXIÓN A POSTGRESQL / SUPABASE
# ==========================================================

def get_connection():
    return psycopg2.connect(
        st.secrets["DATABASE_URL"],
        sslmode="require"
    )

# ==========================================================
# UTILIDADES DE CONTRASEÑA
# ==========================================================

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

# ==========================================================
# CONSULTAS A LA BASE DE DATOS
# ==========================================================

def get_lessons():
    conn = get_connection()
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
    lessons = pd.read_sql_query(query, conn)
    conn.close()
    return lessons

def get_questions(lesson_id: int):
    conn = get_connection()
    query = """
        SELECT *
        FROM questions
        WHERE lesson_id = %s
        ORDER BY order_index, id;
    """
    questions = pd.read_sql_query(query, conn, params=(lesson_id,))
    conn.close()
    return questions

def get_user_progress(user_id: int):
    conn = get_connection()
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
    df = pd.read_sql_query(query, conn, params=(user_id,))
    conn.close()
    return df

# ==========================================================
# REGISTRO E INICIO DE SESIÓN DIRECTO
# ==========================================================

def register_student(registration_code, first_name, last_name):
    registration_code = registration_code.strip()
    first_name = first_name.strip()
    last_name = last_name.strip()

    if not registration_code or not first_name or not last_name:
        return False, "Por favor completa tu número de registro, nombre y apellido."

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT id FROM users WHERE registration_code = %s;", (registration_code,))
        if cur.fetchone():
            return False, "Este número de registro ya existe. Puedes iniciar sesión directamente."

        # La contraseña es directamente el número de registro
        initial_password_hash = hash_password(registration_code)

        cur.execute("""
            INSERT INTO users (
                registration_code,
                first_name,
                last_name,
                password_hash,
                role,
                must_change_password
            )
            VALUES (%s, %s, %s, %s, 'student', FALSE);
        """, (registration_code, first_name, last_name, initial_password_hash))

        conn.commit()
        return True, "¡Cuenta creada exitosamente! Tu usuario y tu clave son tu número de registro."

    except Exception as error:
        if conn:
            conn.rollback()
        return False, f"Error al registrar: {error}"
    finally:
        if conn:
            conn.close()

def login_student(registration_code, password):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, registration_code, first_name, last_name, password_hash, role
            FROM users
            WHERE registration_code = %s;
        """, (registration_code.strip(),))

        user = cur.fetchone()
        if not user:
            return None, "No existe un usuario con ese registro."

        if not verify_password(password, user[4]):
            return None, "Registro o contraseña incorrectos."

        user_data = {
            "id": user[0],
            "registration_code": user[1],
            "first_name": user[2],
            "last_name": user[3],
            "role": user[5]
        }
        return user_data, None

    except Exception as error:
        return None, f"Error al iniciar sesión: {error}"
    finally:
        if conn:
            conn.close()

# ==========================================================
# UTILIDADES DE IMAGEN (DIBUJO)
# ==========================================================

def image_to_base64(image_data):
    image = Image.fromarray(image_data.astype("uint8"))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")

def uploaded_image_to_base64(uploaded_file):
    image_bytes = uploaded_file.getvalue()
    return base64.b64encode(image_bytes).decode("utf-8")

# ==========================================================
# GUARDAR EVALUACIÓN Y PROGRESO
# ==========================================================

def save_quiz_attempt(user_id, lesson_id, questions, student_answers):
    total_points = 0
    obtained_points = 0
    has_drawing_question = False
    drawing_was_sent = False

    # Calcular puntaje:
    # - Alternativas: se califican normalmente.
    # - Texto abierto: se registra, pero no suma puntaje automático.
    # - Dibujo: cualquier dibujo enviado vale como correcto.
    for _, question in questions.iterrows():
        question_id = int(question["id"])
        question_type = question["question_type"]
        points = int(question["points"])
        answer = student_answers.get(question_id, {})

        if question_type == "multiple_choice":
            total_points += points

            selected_option = answer.get("selected_option")

            if selected_option == question["correct_option"]:
                obtained_points += points

        elif question_type == "drawing":
            total_points += points
            has_drawing_question = True

            image_base64 = answer.get("image_base64")

            # Si se mandó un dibujo, se acepta como correcto.
            if image_base64:
                obtained_points += points
                drawing_was_sent = True

    # Si no existen preguntas calificables, queda en 0.
    if total_points > 0:
        score = round((obtained_points / total_points) * 100, 2)
    else:
        score = 0

    # La nota mínima para aprobar es 70%.
    passed = score >= 70

    conn = None

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Crear el intento de evaluación
        cur.execute("""
            INSERT INTO quiz_attempts (
                user_id,
                lesson_id,
                score,
                passed
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """, (
            user_id,
            lesson_id,
            score,
            passed
        ))

        attempt_id = cur.fetchone()[0]

        # Guardar cada respuesta del estudiante
        for _, question in questions.iterrows():
            question_id = int(question["id"])
            question_type = question["question_type"]
            points = int(question["points"])
            answer = student_answers.get(question_id, {})

            # Pregunta de alternativas
            if question_type == "multiple_choice":
                selected_option = answer.get("selected_option")

                is_correct = (
                    selected_option == question["correct_option"]
                )

                points_obtained = points if is_correct else 0

                cur.execute("""
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (
                    attempt_id,
                    question_id,
                    selected_option,
                    is_correct,
                    points_obtained
                ))

            # Pregunta abierta: queda registrada para el docente
            elif question_type == "open_text":
                text_answer = answer.get("text_answer", "")

                cur.execute("""
                    INSERT INTO question_answers (
                        attempt_id,
                        question_id,
                        selected_option,
                        text_answer,
                        is_correct,
                        points_obtained
                    )
                    VALUES (%s, %s, NULL, %s, NULL, 0);
                """, (
                    attempt_id,
                    question_id,
                    text_answer
                ))

            # Pregunta de dibujo: cualquier dibujo enviado es correcto
            elif question_type == "drawing":
                image_base64 = answer.get("image_base64")

                if image_base64:
                    cur.execute("""
                        INSERT INTO drawing_answers (
                            attempt_id,
                            question_id,
                            image_base64,
                            teacher_feedback,
                            reviewed,
                            points_obtained
                        )
                        VALUES (%s, %s, %s, %s, TRUE, %s);
                    """, (
                        attempt_id,
                        question_id,
                        image_base64,
                        'Dibujo recibido y aceptado automáticamente.',
                        points
                    ))

        # Guardar o actualizar el progreso del usuario
        cur.execute("""
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
                best_score = GREATEST(
                    user_progress.best_score,
                    EXCLUDED.best_score
                ),
                is_completed = (
                    user_progress.is_completed
                    OR EXCLUDED.is_completed
                ),
                completed_at = CASE
                    WHEN user_progress.completed_at IS NULL
                         AND EXCLUDED.is_completed
                    THEN CURRENT_TIMESTAMP
                    ELSE user_progress.completed_at
                END,
                updated_at = CURRENT_TIMESTAMP;
        """, (
            user_id,
            lesson_id,
            score,
            passed,
            passed
        ))

        conn.commit()

        return True, {
            "score": score,
            "passed": passed,
            "drawing_was_sent": drawing_was_sent,
            "message": (
                "El dibujo fue recibido y aceptado automáticamente."
                if has_drawing_question and drawing_was_sent
                else "Evaluación registrada correctamente."
            )
        }

    except Exception as error:
        if conn:
            conn.rollback()

        return False, f"Error al guardar la evaluación: {error}"

    finally:
        if conn:
            conn.close()

    auto_gradable_questions = 0
    auto_correct_points = 0
    auto_total_points = 0

    for _, question in questions.iterrows():
        if question["question_type"] == "multiple_choice":
            auto_gradable_questions += 1
            auto_total_points += int(question["points"])
            answer = student_answers.get(int(question["id"]), {})
            selected = answer.get("selected_option")
            if selected == question["correct_option"]:
                auto_correct_points += int(question["points"])

    if auto_gradable_questions > 0 and auto_total_points > 0:
        score = round((auto_correct_points / auto_total_points) * 100, 2)
    else:
        score = 0

    passed = score >= 70 and auto_gradable_questions > 0
    conn = None

    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO quiz_attempts (user_id, lesson_id, score, passed)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """, (user_id, lesson_id, score, passed))

        attempt_id = cur.fetchone()[0]

        for _, question in questions.iterrows():
            question_id = int(question["id"])
            question_type = question["question_type"]
            answer = student_answers.get(question_id, {})

            if question_type == "multiple_choice":
                selected_option = answer.get("selected_option")
                is_correct = (selected_option == question["correct_option"])
                points_obtained = int(question["points"]) if is_correct else 0
                cur.execute("""
                    INSERT INTO question_answers (attempt_id, question_id, selected_option, is_correct, points_obtained)
                    VALUES (%s, %s, %s, %s, %s);
                """, (attempt_id, question_id, selected_option, is_correct, points_obtained))

            elif question_type == "open_text":
                cur.execute("""
                    INSERT INTO question_answers (attempt_id, question_id, text_answer)
                    VALUES (%s, %s, %s);
                """, (attempt_id, question_id, answer.get("text_answer", "")))

            elif question_type == "drawing":
                image_base64 = answer.get("image_base64")
                if image_base64:
                    cur.execute("""
                        INSERT INTO drawing_answers (attempt_id, question_id, image_base64)
                        VALUES (%s, %s, %s);
                    """, (attempt_id, question_id, image_base64))

        cur.execute("""
            INSERT INTO user_progress (
                user_id, lesson_id, best_score, is_completed, completed_at, updated_at
            )
            VALUES (%s, %s, %s, %s, CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END, CURRENT_TIMESTAMP)
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
        """, (user_id, lesson_id, score, passed, passed))

        conn.commit()
        return True, {"score": score, "passed": passed}

    except Exception as error:
        if conn:
            conn.rollback()
        return False, f"Error al guardar la evaluación: {error}"
    finally:
        if conn:
            conn.close()

# ==========================================================
# GENERACIÓN DE PDF
# ==========================================================

def generate_progress_pdf(full_name, registration_code, progress_df):
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=landscape(letter))

    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawCentredString(396, 540, "REPORTE DE PROGRESO DE MINI LECCIONES")

    pdf.setFont("Helvetica", 12)
    pdf.drawString(70, 495, f"Estudiante: {full_name}")
    pdf.drawString(70, 475, f"Registro: {registration_code}")
    pdf.drawString(70, 455, f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

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

        module_name = str(row["modulo"])[:22]
        lesson_name = str(row["leccion"])[:35]
        score = f"{float(row['mejor_puntaje']):.0f}%"
        status = "Completado" if row["completado"] else "Pendiente"

        pdf.drawString(70, y, module_name)
        pdf.drawString(230, y, lesson_name)
        pdf.drawString(490, y, score)
        pdf.drawString(580, y, status)
        y -= 22

    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()

# ==========================================================
# INTERFAZ DE USUARIO (STREAMLIT)
# ==========================================================

if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.title("🎓 Sistema de Mini Lecciones")
    st.write("Tu clave de ingreso es siempre tu **número de registro**.")

    tab_login, tab_register = st.tabs(["🔐 Iniciar Sesión", "📝 Registrarse"])

    with tab_login:
        st.subheader("Acceso al Sistema")
        login_registration = st.text_input("Número de Registro", key="login_reg")
        login_password = st.text_input("Contraseña (tu número de registro)", type="password", key="login_pwd")

        if st.button("Ingresar", use_container_width=True, type="primary"):
            user, error = login_student(login_registration, login_password)
            if error:
                st.error(error)
            else:
                st.session_state.user = user
                st.rerun()

    with tab_register:
        st.subheader("Registro de Nuevo Estudiante")
        reg_code = st.text_input("Número de Registro / Código", key="reg_code")
        reg_first_name = st.text_input("Nombre", key="reg_first")
        reg_last_name = st.text_input("Apellido", key="reg_last")

        st.caption("Nota: Tu contraseña para ingresar será exactamente tu número de registro.")

        if st.button("Crear Cuenta", use_container_width=True):
            success, message = register_student(reg_code, reg_first_name, reg_last_name)
            if success:
                st.success(message)
            else:
                st.error(message)

else:
    user = st.session_state.user
    user_full_name = f"{user['first_name']} {user['last_name']}"

    st.sidebar.title("🎓 Menú")
    st.sidebar.write(f"👤 **{user_full_name}**")
    st.sidebar.caption(f"Registro: `{user['registration_code']}`")

    menu = st.sidebar.radio(
        "Ir a:",
        ["📚 Lecciones", "📊 Mi Progreso", "🚪 Cerrar Sesión"]
    )

    if menu == "🚪 Cerrar Sesión":
        st.session_state.user = None
        st.rerun()

    elif menu == "📊 Mi Progreso":
        st.header("📊 Mi Progreso Académico")
        progress = get_user_progress(user["id"])

        if progress.empty:
            st.info("Aún no tienes evaluaciones registradas. Comienza resolviendo una lección.")
        else:
            total_lessons = len(progress)
            completed = int(progress["completado"].sum())
            average = float(progress["mejor_puntaje"].mean())
            completion_percent = (completed / total_lessons) * 100

            col1, col2, col3 = st.columns(3)
            col1.metric("Lecciones Completadas", f"{completed}/{total_lessons}")
            col2.metric("Avance Total", f"{completion_percent:.1f}%")
            col3.metric("Promedio de Puntaje", f"{average:.1f}%")

            st.dataframe(progress, use_container_width=True)

            # Exportación a Excel
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                progress.to_excel(writer, index=False, sheet_name="Progreso")

            st.download_button(
                label="📥 Descargar Progreso en Excel",
                data=excel_buffer.getvalue(),
                file_name=f"progreso_{user['registration_code']}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            # Exportación a PDF
            pdf_data = generate_progress_pdf(user_full_name, user["registration_code"], progress)
            st.download_button(
                label="📄 Descargar Progreso en PDF",
                data=pdf_data,
                file_name=f"progreso_{user['registration_code']}.pdf",
                mime="application/pdf"
            )

    elif menu == "📚 Lecciones":
        st.header("📚 Mini Lecciones")
        lessons = get_lessons()

        if lessons.empty:
            st.warning("No hay lecciones cargadas en la base de datos.")
        else:
            lessons["label"] = lessons["module_title"] + " — " + lessons["lesson_title"]
            selected_label = st.selectbox("Selecciona una lección", lessons["label"])
            lesson = lessons[lessons["label"] == selected_label].iloc[0]

            st.subheader(lesson["lesson_title"])
            st.caption(f"Módulo: {lesson['module_title']} | Duración: {lesson['duration_minutes']} min")

            if lesson["video_url"]:
                st.video(lesson["video_url"])

            if lesson["content"]:
                st.markdown(lesson["content"])

            st.divider()
            st.subheader("📝 Evaluación Interactiva")
            questions = get_questions(int(lesson["id"]))

            if questions.empty:
                st.info("Esta lección no tiene preguntas configuradas.")
            else:
                student_answers = {}

                for idx, question in questions.iterrows():
                    question_id = int(question["id"])
                    q_type = question["question_type"]

                    st.markdown(f"**Pregunta {idx + 1}: {question['question_text']}**")

                    if q_type == "multiple_choice":
                        opts = []
                        if pd.notna(question["option_a"]): opts.append(f"A) {question['option_a']}")
                        if pd.notna(question["option_b"]): opts.append(f"B) {question['option_b']}")
                        if pd.notna(question["option_c"]): opts.append(f"C) {question['option_c']}")
                        if pd.notna(question["option_d"]): opts.append(f"D) {question['option_d']}")

                        selected = st.radio("Elige una opción:", opts, key=f"mc_{question_id}")
                        student_answers[question_id] = {"selected_option": selected[0]}

                    elif q_type == "open_text":
                        text_resp = st.text_area("Escribe tu respuesta:", key=f"txt_{question_id}", height=100)
                        student_answers[question_id] = {"text_answer": text_resp}

                    elif q_type == "drawing":
                        st.info("Dibuja tu respuesta en el recuadro interactivo:")
                        img_b64 = None

                        if CANVAS_AVAILABLE:
                            col_tool1, col_tool2 = st.columns(2)
                            with col_tool1:
                                tool_mode = st.selectbox("Herramienta", ["freedraw", "line", "rect", "circle"], key=f"tool_{question_id}")
                            with col_tool2:
                                tool_color = st.color_picker("Color", "#000000", key=f"col_{question_id}")

                            canvas_res = st_canvas(
                                fill_color="rgba(255, 255, 255, 0.0)",
                                stroke_width=3,
                                stroke_color=tool_color,
                                background_color="#FFFFFF",
                                height=300,
                                width=600,
                                drawing_mode=tool_mode,
                                update_streamlit=True,
                                key=f"cnv_{question_id}"
                            )
                            if canvas_res.image_data is not None:
                                img_b64 = image_to_base64(canvas_res.image_data)
                        else:
                            uploaded = st.file_uploader("Sube una imagen con tu dibujo", type=["png", "jpg", "jpeg"], key=f"up_{question_id}")
                            if uploaded is not None:
                                img_b64 = uploaded_image_to_base64(uploaded)

                        student_answers[question_id] = {"image_base64": img_b64}

                    st.divider()

                if st.button("✅ Enviar Respuestas", type="primary", use_container_width=True):
                    success, res = save_quiz_attempt(user["id"], int(lesson["id"]), questions, student_answers)
                   if success:
                        st.success(
                            f"¡Evaluación registrada! "
                            f"Puntaje obtenido: {res['score']:.0f}%"
                        )
                    
                        if res.get("drawing_was_sent"):
                            st.info("✅ Tu dibujo fue recibido y aceptado como correcto.")
                    
                        if res["passed"]:
                            st.balloons()
                            st.success("🎉 ¡Aprobaste la mini lección!")
                        else:
                            st.warning(
                                "Aún no alcanzas el 70%. "
                                "Puedes volver a intentar el cuestionario."
                            )
                    else:
                        st.error(res)
